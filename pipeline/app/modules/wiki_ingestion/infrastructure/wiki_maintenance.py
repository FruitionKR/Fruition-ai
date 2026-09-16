from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from app.core.llm_env import resolve_llm_provider_defaults
from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import (
    BgeM3EmbeddingModel,
)
from app.modules.wiki_embedding.infrastructure.postgres_wiki_page_embedding_repository import (
    reserve_page_embeddings,
)
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)
from app.modules.wiki_ingestion.application.models import (
    WikiMaintenanceCommand,
    WikiMaintenanceConfigurationError,
)
from app.modules.wiki_ingestion.application.ports import (
    WikiEmbeddingJobPort,
    WikiMaintenancePort,
)
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database
from app.modules.wiki_ingestion.infrastructure.object_storage import delete_object
from app.modules.wiki_ingestion.infrastructure.promotion_concept_page import (
    build_promotion_concept_page,
    promotion_representative,
)
from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import (
    concept_write_lock,
)


logger = logging.getLogger(__name__)


class PostgresWikiMaintenance(WikiMaintenancePort):
    def __init__(self, embedding_job: WikiEmbeddingJobPort | None = None) -> None:
        self._embedding_job = embedding_job

    def lint(self, command: WikiMaintenanceCommand) -> dict[str, Any]:
        if not command.dry_run and not str(command.operation_id or "").strip():
            raise WikiMaintenanceConfigurationError(
                "operation_id is required when dry_run is false"
            )
        should_materialize = command.materialize_promotions and not command.dry_run
        promotion_generator = (
            self._build_promotion_page_generator(command)
            if should_materialize
            else None
        )
        lock = (
            concept_write_lock(command.workspace_id, str(command.operation_id))
            if not command.dry_run
            else nullcontext()
        )
        transaction = (
            nullcontext(None) if command.dry_run else database.connect()
        )
        with lock, transaction as connection:
            result = database.lint_wiki_workspace(
                command.user_id,
                command.workspace_id,
                materialize_promotions=should_materialize,
                promotion_page_generator=promotion_generator,
                apply_reconciliation=not command.dry_run,
                operation_id=command.operation_id,
                write_log=False,
                connection=connection,
            )
            result.update(
                database.lint_orphan_wiki_links(
                    command.user_id,
                    command.workspace_id,
                    apply=not command.dry_run,
                    connection=connection,
                )
            )
            if not command.dry_run:
                result["operation_id"] = command.operation_id
                written_object_keys: list[str] = []
                try:
                    operation_artifacts = database.persist_lint_operation_result(
                        command.user_id,
                        command.workspace_id,
                        str(command.operation_id),
                        result,
                        connection=connection,
                    )
                    written_object_keys.extend(
                        key
                        for artifact in operation_artifacts
                        for key in (
                            artifact.get("markdown_key"),
                            artifact.get("contribution_key"),
                        )
                        if key
                    )
                    result["operation_artifacts"] = operation_artifacts
                    result["changed_pages"] = operation_artifacts
                    log_path = database.write_wiki_lint_log(result)
                    if log_path:
                        written_object_keys.append(log_path)
                    written_object_keys.extend(
                        database.apply_lint_object_changes(result) or []
                    )
                    page_ids = _embedding_page_ids(result)
                    if self._embedding_job is not None and page_ids:
                        reserve_page_embeddings(
                            connection,
                            page_ids,
                            BgeM3EmbeddingModel().model_name,
                        )
                except Exception:
                    # 이후 단계 실패로 DB 트랜잭션이 롤백될 때, 이미 object storage에
                    # 써버린 lint 산출물이 orphan으로 남지 않도록 함께 지운다.
                    for key in written_object_keys:
                        delete_object(key)
                    raise
        page_ids = _embedding_page_ids(result)
        if not command.dry_run and self._embedding_job is not None and page_ids:
            try:
                self._embedding_job.start(str(command.operation_id), page_ids)
            except Exception:
                logger.warning(
                    "wiki embedding worker start failed; pending reservation remains operation_id=%s",
                    command.operation_id,
                    exc_info=True,
                )
        return result

    def _build_promotion_page_generator(
        self,
        command: WikiMaintenanceCommand,
    ) -> Callable[[dict[str, Any]], dict[str, Any]]:
        client = _lint_api_client(command)

        def generate(cluster: dict[str, Any]) -> dict[str, Any]:
            representative = promotion_representative(cluster)
            allowed_refs = {
                ref
                for claim in cluster.get("claims", [])
                for ref in claim.get("refs", [])
            }
            allowed_refs.update(
                block.get("ref")
                for block in cluster.get("source_blocks", [])
                if block.get("ref")
            )
            source_ref_by_block = {ref.rsplit(":", 1)[-1]: ref for ref in allowed_refs}
            user_payload = {
                "cluster": {
                    "id": cluster.get("id"),
                    "representative": representative,
                    "promotion_status": cluster.get("promotion_status"),
                    "promotion_source_refs": cluster.get("promotion_source_refs", []),
                    "claims": cluster.get("claims", []),
                    "relations": cluster.get("relations", []),
                },
                "source_blocks": cluster.get("source_blocks", []),
                "allowed_anchor_refs": sorted(allowed_refs),
            }
            draft = client.complete_json(
                _promotion_concept_system_prompt(),
                json.dumps(user_payload, ensure_ascii=False, indent=2),
            )
            return build_promotion_concept_page(
                cluster,
                draft,
                allowed_refs,
                source_ref_by_block,
            )

        return generate


def _lint_api_client(command: WikiMaintenanceCommand) -> ChatCompletionsJsonClient:
    defaults = resolve_llm_provider_defaults(provider=command.provider, model=command.model)
    if not defaults.api_key:
        raise WikiMaintenanceConfigurationError(
            f"Missing API key. Set {defaults.api_key_env}"
        )
    if not defaults.model:
        raise WikiMaintenanceConfigurationError(
            "Missing model"
        )
    return ChatCompletionsJsonClient(
        ChatClientConfig(
            api_key=defaults.api_key,
            model=defaults.model,
            temperature=None,
            timeout_seconds=180,
            max_tokens=None,
            json_mode=False,
            provider=defaults.provider,
        )
    )


def _promotion_concept_system_prompt() -> str:
    base_prompt = Path("prompts/concept_page_generation.system.md").read_text(
        encoding="utf-8"
    )
    return (
        base_prompt
        + "\n\nStage=PromotionClusterConceptPageGeneration.\n"
        "You receive one promotion cluster, evidence claims, existing relation candidates, and source blocks.\n"
        "Generate a real concept page draft from the supplied evidence only.\n"
        "Use allowed_anchor_refs exactly as anchor_block_ids. They may be global refs like doc_id:B0001.\n"
        "Do not use refs that are not listed in allowed_anchor_refs.\n"
    )


def _embedding_page_ids(result: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            str(promotion["page_id"])
            for promotion_type in ("materialized_promotions", "merged_promotions")
            for promotion in result.get(promotion_type, [])
            if isinstance(promotion, dict) and promotion.get("page_id")
        )
    )
