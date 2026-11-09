from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from typing import Any

from app.core.llm_env import resolve_llm_provider_defaults
from app.modules.wiki_embedding.application.ports import EmbeddingModelPort
from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import (
    BgeM3EmbeddingModel,
)
from app.modules.wiki_embedding.infrastructure.postgres_wiki_page_embedding_repository import (
    reserve_page_embeddings,
)
from app.modules.wiki_generation.application.judge_candidates import PartOfJudge, llm_part_of_judge
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
    RetryOnceJsonCompletion,
)
from app.modules.wiki_generation.infrastructure.jev_concept_resolver import (
    JevConceptResolver,
    embedding_server_model,
    jev_concept_judge,
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
from app.modules.task_cancellation.infrastructure.object_change_journal import (
    snapshot as snapshot_object,
)
from app.modules.wiki_ingestion.infrastructure.object_storage import (
    client as storage_client,
    delete_object,
    split_storage_uri,
    write_text_object,
)
from app.modules.wiki_ingestion.infrastructure.promotion_concept_page import (
    build_promotion_concept_page,
    promotion_representative,
)
from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import (
    concept_write_lock,
)
from app.modules.wiki_ingestion.infrastructure.pending_contributions import record_pending_deltas
from app.modules.wiki_ingestion.infrastructure.wiki_publication import (
    finalize_publication,
    normalize_operation_result,
    prepare_publication,
    publication_id,
    write_publication_text,
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
        if not command.dry_run:
            prepare_publication(
                str(command.operation_id),
                command.workspace_id,
                command.user_id,
                "LINT",
                database_connect=database.connect,
            )
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
        concept_judge = jev_concept_judge() if not command.dry_run else None
        judge_part_of, embedding_model = _part_of_judge(command, concept_judge) if not command.dry_run else (None, None)
        with lock, transaction as connection:
            result = database.lint_wiki_workspace(
                command.user_id,
                command.workspace_id,
                materialize_promotions=should_materialize,
                promotion_page_generator=promotion_generator,
                concept_judge=concept_judge if should_materialize else None,
                judge_part_of=judge_part_of if should_materialize else None,
                embedding_model=embedding_model if should_materialize else None,
                apply_reconciliation=not command.dry_run,
                operation_id=command.operation_id,
                write_log=False,
                connection=connection,
            )
            result.update(
                database.lint_orphan_wiki_links(
                    command.workspace_id,
                    apply=not command.dry_run,
                    connection=connection,
                )
            )
            result.update(
                database.lint_concept_entries(
                    command.user_id,
                    command.workspace_id,
                    command.operation_id,
                    apply=not command.dry_run,
                    promotion_changes=result.get("_lint_page_changes"),
                    connection=connection,
                    concept_judge=concept_judge,
                    judge_part_of=judge_part_of,
                    embedding_model=embedding_model,
                )
            )
            if not command.dry_run:
                result["operation_id"] = command.operation_id
                lint_date = date.today().isoformat()
                # 클러스터·로그는 편입도 덧붙이는 워크스페이스 공유 객체다. 실패하면 지우지 않고
                # 쓰기 전 내용으로 되돌린다. 편입·post-ingest도 같은 concept_write_lock 아래서 쓰므로
                # 보관한 내용과 되돌리는 사이에 다른 쓰기가 끼지 않는다.
                shared_before = {
                    key: _snapshot_object(key)
                    for key in database.lint_shared_object_keys(command.workspace_id, lint_date)
                }
                try:
                    operation_artifacts = database.persist_lint_operation_result(
                        command.workspace_id,
                        str(command.operation_id),
                        result,
                        connection=connection,
                        artifact_writer=(
                            lambda key, text, content_type: write_publication_text(
                                str(command.operation_id),
                                key.split("/pages/", 1)[1].split("/", 1)[0],
                                key,
                                text,
                                content_type,
                                database_connect=database.connect,
                            )
                        ),
                    )
                    result["operation_artifacts"] = operation_artifacts
                    result["changed_pages"] = operation_artifacts
                    database.write_wiki_lint_log(result, lint_date)
                    database.apply_lint_object_changes(result)
                    page_ids = _embedding_page_ids(result)
                    if self._embedding_job is not None and page_ids:
                        reserve_page_embeddings(
                            connection,
                            page_ids,
                            BgeM3EmbeddingModel().model_name,
                        )
                    publication_result = normalize_operation_result({
                        "operation_id": str(command.operation_id),
                        "operation_type": "lint",
                        "status": "succeeded",
                        "workspace_id": command.workspace_id,
                        "user_id": command.user_id,
                        "target_document_id": None,
                        "summary": f"Wiki lint로 페이지 {len(operation_artifacts)}개를 변경했습니다.",
                        "changed_pages": [
                            {**artifact, "contribution_stored": False}
                            for artifact in operation_artifacts
                        ],
                        "failed_pages": [],
                        "deleted_pages": result.get("deleted_pages") or [],
                        "link_changes": None,
                        "failed_actions": None,
                        "ledger_contributions": result.get("ledger_contributions") or [],
                    })
                    pending = [
                        {
                            "page_id": item["page_id"],
                            "operation_id": item["operation_id"],
                            "document_id": item.get("document_id"),
                            "object_key": item["contribution_key"],
                            "active": True,
                            "linked_by_operation_id": item.get("linked_by_operation_id"),
                        }
                        for item in publication_result["ledger_contributions"]
                    ]
                    record_pending_deltas(
                        connection,
                        command.workspace_id,
                        pending,
                        publication_id=publication_id(str(command.operation_id)),
                    )
                    event = finalize_publication(
                        connection,
                        operation_id=str(command.operation_id),
                        workspace_id=command.workspace_id,
                        user_id=command.user_id,
                        kind="LINT",
                        outcome=(
                            "PUBLISHED"
                            if operation_artifacts or publication_result["deleted_pages"]
                            else "NO_CHANGE"
                        ),
                        result=publication_result,
                    )
                    result["publication_result"] = event["result"]
                except Exception:
                    for key, before in shared_before.items():
                        _restore_object(key, before)
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


def _snapshot_object(key: str) -> dict[str, str] | None:
    bucket, object_name = split_storage_uri(key)
    return snapshot_object(storage_client(), bucket, object_name)


def _restore_object(key: str, before: dict[str, str] | None) -> None:
    if before is None:
        delete_object(key)
    else:
        write_text_object(key, before["text"], before["content_type"])


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


def _part_of_judge(
    command: WikiMaintenanceCommand,
    concept_judge: JevConceptResolver | None,
) -> tuple[PartOfJudge | None, EmbeddingModelPort | None]:
    """페이지 이동(ADR 0042)의 part_of 판정기와 후보를 고를 임베딩 모델. 편입 배정과 같은 판정기·모델·캐시를 쓴다.

    Jev가 켜져 있으면 Jev, 아니면 이 lint 작업의 LLM이 같은 프롬프트로 판정한다. 임베딩 서버나 LLM 설정이 없으면
    옮기지 않는다(lint는 실패시키지 않는다).
    """
    model = embedding_server_model()
    if model is None:
        return None, None
    if concept_judge is not None:
        return concept_judge.judge_part_of, model
    try:
        client = _lint_api_client(command)
    except WikiMaintenanceConfigurationError as exc:
        logger.warning("[lint 페이지 이동 판정 꺼짐] %s", exc)
        return None, None
    prompt = Path("prompts/concept_part_of.system.md").read_text(encoding="utf-8")
    return llm_part_of_judge(RetryOnceJsonCompletion(client), prompt), model


def _promotion_concept_system_prompt() -> str:
    base_prompt = Path("prompts/concept_page_generation.system.md").read_text(
        encoding="utf-8"
    )
    return (
        base_prompt
        + "\n\nStage=PromotionClusterConceptPageGeneration.\n"
        "You receive one promotion cluster, evidence claims, and source blocks.\n"
        "Generate a real concept page draft from the supplied evidence only.\n"
        "Use allowed_anchor_refs exactly as anchor_block_ids. They may be global refs like doc_id:B0001.\n"
        "Do not use refs that are not listed in allowed_anchor_refs.\n"
    )


def _embedding_page_ids(result: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            str(promotion["page_id"])
            for promotion_type in ("materialized_promotions", "merged_promotions", "ledger_contributions")
            for promotion in result.get(promotion_type, [])
            if isinstance(promotion, dict) and promotion.get("page_id")
            and str(promotion["page_id"]) not in result.get("deleted_pages", [])
        )
    )
