"""Query·Agent·maintenance Kafka command worker."""

from __future__ import annotations

from app.modules.agent_run.infrastructure.postgres_agent_run_repository import agent_command_hash as _agent_command_hash
from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository

import asyncio
import hashlib
import json
import logging
import os
import signal
from itertools import count
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from psycopg.types.json import Json

from app.core.llm_env import api_key_from_env, resolve_llm_selection
from app.core.pipeline_control import PipelineRunCancelledError, ensure_task_active, task_cancellation_scope
from app.modules.agent.domain.exceptions import (
    AgentConfigurationError,
    AgentTurnRouteContractError,
)
from app.modules.agent.interfaces.http.dependencies import build_handle_agent_turn_use_case
from app.modules.agent.interfaces.http.routes import _to_response as agent_to_response
from app.modules.agent.interfaces.http.schemas import AgentTurnRequestBody
from app.modules.markdown_edit.domain.markdown_output_contract import (
    MarkdownCreateOutputContractError,
    MarkdownOutputContractError,
)
from app.modules.markdown_edit.domain.markdown_target_scope import MarkdownTargetBoundaryError
from app.modules.query.application.ports import QueryEventPublisherPort
from app.modules.query.domain.entities import ConversationContext, ConversationMessage
from app.modules.query.interfaces.http.dependencies import build_answer_query_use_case
from app.modules.query.interfaces.http.routes import _to_response as query_to_response
from app.modules.query.infrastructure.postgres_wiki_repository import PostgresWikiRepository
from app.modules.skill.domain.exceptions import SkillDisabledError, SkillNotFoundError
from app.modules.wiki_embedding.application.build_wiki_page_embeddings import (
    embedding_result,
)
from app.modules.wiki_embedding.infrastructure.threaded_wiki_embedding_job import (
    build_wiki_embeddings,
)
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)
from app.modules.wiki_generation.infrastructure.post_ingest_cluster import (
    build_post_ingest_cluster_artifact,
)
from app.modules.wiki_generation.infrastructure.post_ingest_quality import (
    evaluate_post_ingest_evidence,
    generate_post_ingest_quality_cases,
    retrieval_reference_metrics,
)
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import (
    read_optional_text_object,
)
from app.modules.wiki_ingestion.interfaces.http.dependencies import (
    get_restore_wiki_pages_use_case,
    get_wiki_embedding_job,
    get_wiki_maintenance,
)
from app.modules.wiki_ingestion.interfaces.http.schemas import (
    IngestOperationRestoreIn,
    LintOperationRestoreIn,
    WikiLintIn,
    WikiLintOut,
)
from app.modules.wiki_ingestion.infrastructure.backend_document_reader import read_contributions
from app.workers.event_request import without_top_level_secrets


logger = logging.getLogger("task_worker")
BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
COMMAND_TOPIC = os.environ.get("AI_COMMAND_TOPIC", "ai.query.command")
RESULT_TOPIC = os.environ.get("AI_TASK_EVENT_TOPIC", "ai.task.event")
GROUP_ID = os.environ.get("AI_COMMAND_CONSUMER_GROUP", f"{COMMAND_TOPIC}-worker")
MAX_POLL_INTERVAL_MS = int(os.environ.get("AI_TASK_MAX_POLL_INTERVAL_MS", "1800000"))
POST_INGEST_MAX_ATTEMPTS = 3
POST_INGEST_AUDIT_LIMIT = 3


def _required(command: dict[str, Any], *fields: str) -> None:
    missing = [
        field
        for field in fields
        if command.get(field) in (None, "")
        or isinstance(command.get(field), str)
        and not command[field].strip()
    ]
    if missing:
        raise ValueError(f"AI command requires: {', '.join(missing)}")


def _text_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _conversation_context(command: dict[str, Any]) -> ConversationContext | None:
    """대화 맥락은 두 겹이다. 요약은 이전 턴까지의 누적본이고 최근 메시지는 그 위에 얹는 원문이다.

    요약을 함께 넘겨야 이번 턴 요약이 앞 내용을 이어받는다. 빠뜨리면 매 턴 최근 메시지만으로
    요약이 새로 쓰여 앞쪽 대화가 사라진다.
    """
    summary = command.get("recent_conversation_summary")
    messages = command.get("recent_messages") or []
    if not summary and not messages:
        return None
    return ConversationContext(
        recent_conversation_summary=summary,
        recent_messages=tuple(
            ConversationMessage(role=message["role"], content=message["content"])
            for message in messages
        ),
    )


def _handle_query(
    command: dict[str, Any],
    event_publisher: QueryEventPublisherPort | None = None,
) -> dict[str, Any]:
    _required(
        command,
        "run_id",
        "workspace_id",
        "user_id",
        "session_id",
        "question",
        "provider",
        "model",
        "allow_web_search",
    )
    if not isinstance(command["allow_web_search"], bool):
        raise ValueError("allow_web_search must be a boolean")
    result = build_answer_query_use_case(
        provider=str(command["provider"]).strip(),
        model=str(command["model"]).strip(),
        allow_web_search=command["allow_web_search"],
        event_publisher=event_publisher,
    ).execute(
        str(command["question"]),
        workspace_id=str(command["workspace_id"]),
        user_id=str(command["user_id"]),
        conversation_context=_conversation_context(command),
        allow_web_search=command["allow_web_search"],
    )
    return query_to_response(result).model_dump(mode="json")


def _handle_agent(
    command: dict[str, Any],
    event_publisher: QueryEventPublisherPort | None = None,
) -> dict[str, Any]:
    # 문서를 열지 않은 턴은 편집 대상이 없어 document_id·base_version·apply_operation_id·
    # editor_snapshot이 오지 않는다. 이때 AI는 chat_answer·conversation_reply·clarify·reject만 낸다.
    _required(command, "run_id", "workspace_id", "user_id", "message")
    run_id = str(command["run_id"])
    state, replay = _register_agent_command(command)
    if state == "completed":
        return replay or {}

    try:
        payload = AgentTurnRequestBody.model_validate({
            "message": command["message"],
            "provider": command.get("provider"),
            "model": command.get("model"),
            # AI가 질의로 판정했을 때만 쓰인다. 편집·Skill 갈래는 무시한다.
            "allow_web_search": command.get("allow_web_search"),
            "skill_mode": command.get("skill_mode", "auto"),
            "skill_id": command.get("skill_id"),
            "workspace_id": command["workspace_id"],
            "user_id": command["user_id"],
            "conversation_context": command.get("conversation_context"),
            "active_markdown_context": command.get("editor_snapshot"),
            "document_id": command.get("document_id"),
            "base_version": command.get("base_version"),
            "skill_draft_sources": command.get("skill_draft_sources", []),
            "skill_draft_user_directives": command.get("skill_draft_user_directives", []),
            "skill_draft_excluded_literals": command.get("skill_draft_excluded_literals", []),
            "skill_scope_type": command.get("skill_scope_type"),
        })
        result = agent_to_response(
            build_handle_agent_turn_use_case(
                provider=payload.provider,
                model=payload.model,
                event_publisher=event_publisher,
                parent_run_id=run_id,
            ).execute(payload.to_domain())
        ).model_dump(mode="json")
    except Exception as exc:
        error_code = _agent_failure_code(exc)
        logger.error(
            "Agent turn 처리 실패: run_id=%s error_code=%s error_type=%s",
            run_id,
            error_code,
            type(exc).__name__,
        )
        with database.connect_ai() as conn:
            conn.execute(
                """
                UPDATE agent_runs SET status = 'failed', error_code = %s,
                    result = %s::jsonb || COALESCE(result, '{}'::jsonb),
                    updated_at = now(), finished_at = now() WHERE id = %s AND status = 'executing'
                """,
                (error_code, Json(_agent_failure_result(exc)), run_id),
            )
            conn.execute(
                "UPDATE agent_jobs SET status = 'failed', updated_at = now() WHERE run_id = %s AND job_type = 'markdown_turn'",
                (run_id,),
            )
        raise
    with database.connect_ai() as conn:
        updated = conn.execute(
            """
            UPDATE agent_runs SET status = 'completed', result = %s::jsonb || COALESCE(result, '{}'::jsonb), error_code = NULL,
                updated_at = now(), finished_at = now() WHERE id = %s AND status = 'executing'
            RETURNING id
            """,
            (Json(result), run_id),
        ).fetchone()
        if updated is not None:
            conn.execute(
                "UPDATE agent_jobs SET status = 'completed', updated_at = now() WHERE run_id = %s AND job_type = 'markdown_turn'",
                (run_id,),
            )
        else:
            # 응답은 발행하지 않지만 복구 worker가 실제 갈래를 확인할 기록은 남긴다.
            conn.execute("UPDATE agent_runs SET result = %s::jsonb || COALESCE(result, '{}'::jsonb), updated_at = now() "
                         "WHERE id = %s AND status IN ('cancel_requested', 'rolling_back')", (Json(result), run_id))
            conn.execute(
                "UPDATE agent_jobs SET status = 'cancelled', updated_at = now() "
                "WHERE run_id = %s AND job_type = 'markdown_turn' AND status = 'executing'",
                (run_id,),
            )
    if updated is None:
        raise PipelineRunCancelledError("Agent run no longer accepts a result.")
    return result


def _agent_failure_code(error: Exception) -> str:
    if isinstance(error, PipelineRunCancelledError):
        return "agent_turn_cancelled"
    if isinstance(error, AgentTurnRouteContractError):
        return "agent_turn_route_contract_failed"
    if isinstance(error, MarkdownCreateOutputContractError):
        return "markdown_create_output_contract_failed"
    if isinstance(error, MarkdownOutputContractError):
        return "markdown_output_contract_failed"
    if isinstance(error, MarkdownTargetBoundaryError):
        return "markdown_target_crosses_structure"
    if isinstance(error, (SkillNotFoundError, SkillDisabledError)):
        return error.code
    if isinstance(error, AgentConfigurationError):
        return "agent_not_configured"
    if isinstance(error, ValueError):
        return "invalid_agent_turn_request"
    return "agent_turn_failed"


def _agent_failure_result(error: Exception) -> dict[str, object]:
    result: dict[str, object] = {
        "outcome": "failed",
        "error_code": _agent_failure_code(error),
        "failure_type": type(error).__name__,
    }
    if isinstance(error, AgentTurnRouteContractError):
        result["contract_failures"] = error.failures
    return result


def _register_agent_command(command: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    run_id = str(command["run_id"])
    provider = model = None
    selection_error: ValueError | None = None
    raw_provider = command.get("provider")
    raw_model = command.get("model")
    if not isinstance(raw_provider, str) or not isinstance(raw_model, str):
        selection_error = ValueError("provider and model are required")
    else:
        try:
            provider, model = resolve_llm_selection(raw_provider, raw_model)
        except ValueError as exc:
            selection_error = exc
    envelope_hash = _agent_command_hash(command)
    deferred_error: ValueError | RuntimeError | None = None
    with database.connect_ai() as conn:
        inserted = conn.execute(
            """
            INSERT INTO agent_runs (
                id, workspace_id, user_id, action, status, request_summary,
                provider, model, document_id, base_version, apply_operation_id,
                command_envelope_hash
            ) VALUES (%s, %s, %s, 'markdown_turn', 'queued', %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """,
            (
                run_id,
                str(command["workspace_id"]),
                str(command["user_id"]),
                str(command["message"])[:1000],
                provider,
                model,
                # 문서를 열지 않은 턴은 셋 다 없다. 컬럼은 nullable이라 그대로 비워 둔다.
                _text_or_none(command.get("document_id")),
                _int_or_none(command.get("base_version")),
                _text_or_none(command.get("apply_operation_id")),
                envelope_hash,
            ),
        ).fetchone()
        if inserted:
            conn.execute(
                """
                INSERT INTO agent_jobs (id, run_id, job_type, status)
                VALUES (%s, %s, 'markdown_turn', 'queued')
                ON CONFLICT (id) DO NOTHING
                """,
                (f"{run_id}:markdown_turn", run_id),
            )
            if selection_error is not None:
                conn.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'failed', error_code = 'invalid_llm_selection',
                        updated_at = now(), finished_at = now()
                    WHERE id = %s
                    """,
                    (run_id,),
                )
                conn.execute(
                    """
                    UPDATE agent_jobs SET status = 'failed', updated_at = now()
                    WHERE run_id = %s AND job_type = 'markdown_turn'
                    """,
                    (run_id,),
                )
        run = conn.execute(
            """
            SELECT status, result, command_envelope_hash
            FROM agent_runs WHERE id = %s AND action = 'markdown_turn' FOR UPDATE
            """,
            (run_id,),
        ).fetchone()
        if run is None or run["command_envelope_hash"] != envelope_hash:
            raise ValueError("Agent command envelope does not match the registered run")
        if selection_error is not None:
            deferred_error = selection_error
        elif run["status"] == "completed":
            return "completed", dict(run["result"] or {})
        elif run["status"] == "failed":
            raise ValueError("Agent run is already failed")
        elif run["status"] in {"cancel_requested", "rolling_back", "rollback_failed", "cancelled"}:
            raise PipelineRunCancelledError("Agent run cancellation has been requested.")
        elif run["status"] == "executing":
            raise RuntimeError("Agent run is already executing")
        else:
            conn.execute(
                "UPDATE agent_runs SET status = 'executing', updated_at = now() WHERE id = %s",
                (run_id,),
            )
            conn.execute(
                "UPDATE agent_jobs SET status = 'executing', updated_at = now() WHERE id = %s",
                (f"{run_id}:markdown_turn",),
            )
    if deferred_error is not None:
        raise deferred_error
    return "execute", None


def _handle_lint(command: dict[str, Any]) -> dict[str, Any]:
    _required(command, "run_id", "workspace_id", "user_id", "provider", "model")
    run_id = str(command["run_id"])
    existing = database.get_pipeline_run(run_id)
    if existing:
        for field in ("workspace_id", "user_id"):
            if str(existing.get(field)) != str(command[field]):
                raise ValueError(f"maintenance command does not match registered {field}")
        if existing.get("status") == "succeeded":
            return dict((existing.get("manifest") or {}).get("task_result") or {})
        if existing.get("status") == "failed":
            raise ValueError(str(existing.get("error") or "maintenance run failed"))
    else:
        database.create_pipeline_run(
            run_id, None, str(command["user_id"]), str(command["workspace_id"]),
            "kafka:lint", f"runs/{run_id}", "lint",
        )
    try:
        lint_payload = {
            field: command.get(field)
            for field in (
                "user_id",
                "workspace_id",
                "operation_id",
                "materialize_promotions",
                "dry_run",
                "provider",
                "model",
            )
            if command.get(field) is not None
        }
        lint_payload["model"] = str(command["model"]).strip()
        lint_payload["provider"] = str(command["provider"]).strip()
        result = get_wiki_maintenance().lint(
            WikiLintIn.model_validate(lint_payload).to_command()
        )
        payload = WikiLintOut.model_validate(result).model_dump(mode="json")
        database.finish_pipeline_run(run_id, {"task_result": payload})
        return payload
    except Exception as exc:
        database.fail_pipeline_run(run_id, str(exc))
        raise


def _validate_restore_contributions(command: dict[str, Any]) -> None:
    expected = command.get("expected_contributions") or {}
    if not expected:
        return
    rows = read_contributions(list(expected), str(command["workspace_id"]))
    current: dict[str, list[str]] = {str(page_id): [] for page_id in expected}
    for row in sorted(rows, key=lambda item: int(item.get("sequence_revision") or 0)):
        page_id = str(row.get("page_id") or "")
        current.setdefault(page_id, []).append(
            f"{row.get('ingest_operation_id')}:{int(row.get('sequence_revision') or 0)}:"
            f"{'1' if row.get('active') else '0'}"
        )
    if current != {str(key): list(value) for key, value in expected.items()}:
        raise ValueError("restore contribution manifest is stale")


def _handle_restore(command: dict[str, Any]) -> dict[str, Any]:
    _required(command, "run_id", "workspace_id", "user_id", "operation_id")
    run_id = str(command["run_id"])
    existing = database.get_pipeline_run(run_id)
    if existing:
        for field in ("workspace_id", "user_id"):
            if str(existing.get(field)) != str(command[field]):
                raise ValueError(f"restore command does not match registered {field}")
        if existing.get("status") == "succeeded":
            return dict((existing.get("manifest") or {}).get("task_result") or {})
        if existing.get("status") == "failed":
            raise ValueError(str(existing.get("error") or "restore run failed"))
    else:
        database.create_pipeline_run(
            run_id, None, str(command["user_id"]), str(command["workspace_id"]),
            f"kafka:{command['kind']}", f"runs/{run_id}", str(command["kind"]),
        )
    try:
        _validate_restore_contributions(command)
        use_case = get_restore_wiki_pages_use_case()
        if command["kind"] == "restore_ingest":
            result = use_case.execute_ingest(
                IngestOperationRestoreIn.model_validate(command).to_command()
            )
        else:
            result = use_case.execute_lint(
                LintOperationRestoreIn.model_validate(command).to_command()
            )
        database.finish_pipeline_run(run_id, {"task_result": result})
        return result
    except Exception as exc:
        database.fail_pipeline_run(run_id, str(exc))
        raise


def _post_ingest_client(command: dict[str, Any]) -> ChatCompletionsJsonClient:
    provider, model = resolve_llm_selection(
        str(command["provider"]).strip(),
        str(command["model"]).strip(),
    )
    api_key = api_key_from_env(provider=provider)
    if not api_key:
        raise RuntimeError(f"{provider} API key가 없습니다.")
    return ChatCompletionsJsonClient(
        ChatClientConfig(
            api_key=api_key,
            model=model,
            temperature=None,
            timeout_seconds=180,
            json_mode=True,
            provider=provider,
        )
    )


def _run_post_ingest_wiki_quality_evaluation(
    command: dict[str, Any],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    if not command.get("quality_required", True):
        return []
    evaluations = list(state.get("wiki_quality_evaluations") or [])
    if evaluations:
        return evaluations
    completion = _post_ingest_client(command)
    if "quality_cases" not in state:
        cases = generate_post_ingest_quality_cases(
            completion=completion,
            source_document_id=str(command["document_id"]),
            source_blocks=database.list_source_blocks(str(command["document_id"])),
            limit=POST_INGEST_AUDIT_LIMIT,
        )
        if not cases:
            raise RuntimeError("원문 기반 RAG 평가 질문을 생성하지 못했습니다.")
        state["quality_cases"] = cases
        database.checkpoint_pipeline_run(str(command["run_id"]), state)
    cases = [
        case
        for case in list(state.get("quality_cases") or [])[:POST_INGEST_AUDIT_LIMIT]
        if isinstance(case, dict) and str(case.get("question") or "").strip()
    ]
    if not cases:
        return []
    use_case = build_answer_query_use_case(
        provider=str(command["provider"]).strip(),
        model=str(command["model"]).strip(),
        allow_web_search=False,
        evaluator_mode="disabled",
        wiki_repository=PostgresWikiRepository(),
    )
    retrieved_cases = []
    source_refs = []
    for case in cases:
        question = str(case["question"])
        result = use_case.retrieve_evidence(
            question,
            workspace_id=str(command["workspace_id"]),
            user_id=str(command["user_id"]),
        )
        evidence_snippets = [
            {
                "rank": snippet.rank,
                "source_document_id": snippet.source_document_id,
                "source_block_ids": snippet.source_block_ids,
                "source_refs": [
                    {
                        "source_document_id": ref.source_document_id,
                        "source_block_id": ref.source_block_id,
                    }
                    for ref in snippet.source_refs
                ],
                "text": snippet.text,
            }
            for snippet in result.evidence_snippets
        ]
        case_source_refs = [
            f"{case.get('source_document_id') or command['document_id']}:{block_id}"
            for block_id in case.get("source_block_ids", [])
        ]
        for snippet in evidence_snippets:
            if snippet["source_refs"]:
                case_source_refs.extend(
                    f"{ref['source_document_id']}:{ref['source_block_id']}"
                    for ref in snippet["source_refs"]
                )
                continue
            case_source_refs.extend(
                f"{snippet['source_document_id']}:{block_id}"
                for block_id in snippet["source_block_ids"]
            )
        source_refs.extend(case_source_refs)
        retrieved_cases.append(
            {
                "question": question,
                "expected_claims": list(case.get("expected_claims") or []),
                "source_document_id": str(
                    case.get("source_document_id") or command["document_id"]
                ),
                "source_block_ids": list(case.get("source_block_ids") or []),
                "evidence_snippets": evidence_snippets,
                "retrieval_summary": {
                    "source_candidate_count": (
                        result.retrieval_summary.source_candidate_count
                    ),
                    "concept_candidate_count": (
                        result.retrieval_summary.concept_candidate_count
                    ),
                    "used_source_count": result.retrieval_summary.used_source_count,
                    "used_concept_count": result.retrieval_summary.used_concept_count,
                },
                **retrieval_reference_metrics(case, evidence_snippets),
            }
        )
    source_blocks = list(command.get("source_blocks") or [])
    if not source_blocks:
        source_blocks = database.list_source_blocks_for_refs(
            list(dict.fromkeys(source_refs))
        )
    judgments = evaluate_post_ingest_evidence(
        completion=completion,
        cases=retrieved_cases,
        source_blocks=source_blocks,
    )
    evaluations = [
        {**retrieved, **judgment}
        for retrieved, judgment in zip(retrieved_cases, judgments, strict=True)
    ]
    state["wiki_quality_evaluations"] = evaluations
    database.checkpoint_pipeline_run(str(command["run_id"]), state)
    return evaluations


def _handle_post_ingest(command: dict[str, Any]) -> dict[str, Any]:
    _required(command, "ingest_run_id")
    ingest_run = database.get_pipeline_run(str(command["ingest_run_id"]))
    ingest_manifest = (ingest_run or {}).get("manifest") or {}
    post_ingest_payload = (
        ingest_manifest.get("post_ingest")
        if ingest_run and ingest_run.get("status") == "succeeded"
        else None
    )
    if not isinstance(post_ingest_payload, dict):
        raise ValueError("post_ingest parent run payload is unavailable")
    command = {**post_ingest_payload, **command}
    _required(
        command,
        "run_id",
        "ingest_run_id",
        "document_id",
        "workspace_id",
        "user_id",
        "provider",
        "model",
        "cluster_normalized",
    )
    if not isinstance(command.get("cluster_normalized"), dict):
        raise ValueError("post_ingest cluster_normalized must be an object")
    if not isinstance(command.get("page_ids", []), list):
        raise ValueError("post_ingest page_ids must be a list")

    run_id = str(command["run_id"])
    existing = database.get_pipeline_run(run_id)
    if existing:
        for field in ("workspace_id", "user_id"):
            if str(existing.get(field)) != str(command[field]):
                raise ValueError(f"post_ingest command does not match registered {field}")
        if existing.get("status") == "succeeded":
            return dict((existing.get("manifest") or {}).get("task_result") or {})
        if existing.get("status") == "failed":
            raise ValueError(str(existing.get("error") or "post_ingest run failed"))
        state = dict(existing.get("manifest") or {})
    else:
        database.create_pipeline_run(
            run_id,
            None,
            str(command["user_id"]),
            str(command["workspace_id"]),
            f"kafka:post_ingest:{command['ingest_run_id']}",
            f"runs/{run_id}",
            "post_ingest",
        )
        state = {}

    attempt = int(state.get("attempt") or 0) + 1
    state["attempt"] = attempt
    database.checkpoint_pipeline_run(run_id, state)
    database.update_pipeline_run_post_ingest(
        str(command["ingest_run_id"]),
        {
            "run_id": run_id,
            "status": "running",
            "attempt": attempt,
            "error": None,
        },
    )
    try:
        if "cluster_artifact" not in state:
            object_name = (
                f"wiki/{command['user_id']}/{command['workspace_id']}"
                "/clusters/active.md"
            )
            existing_active = read_optional_text_object(object_name)
            state["cluster_artifact"] = build_post_ingest_cluster_artifact(
                completion=_post_ingest_client(command),
                normalized=dict(command["cluster_normalized"]),
                existing_active_markdown=existing_active,
                user_id=str(command["user_id"]),
                workspace_id=str(command["workspace_id"]),
                concept_update_decisions=list(
                    command.get("concept_update_decisions") or []
                ),
                core_relation_decisions=list(
                    command.get("core_relation_decisions") or []
                ),
            )
            database.checkpoint_pipeline_run(run_id, state)

        if not state.get("cluster_applied"):
            changed_page_ids = database.persist_post_ingest_clusters(
                str(command["document_id"]),
                str(command["user_id"]),
                str(command["workspace_id"]),
                dict(state["cluster_artifact"]),
            )
            database.update_pipeline_run_cluster_contribution(
                str(command["ingest_run_id"]),
                dict(state["cluster_artifact"]),
            )
            state["page_ids"] = list(
                dict.fromkeys(
                    [
                        *(str(page_id) for page_id in command.get("page_ids", [])),
                        *changed_page_ids,
                    ]
                )
            )
            state["cluster_applied"] = True
            database.checkpoint_pipeline_run(run_id, state)

        if "embedding" not in state:
            page_ids = list(dict.fromkeys(state["page_ids"]))
            if os.environ.get("QUERY_EMBEDDING_MODE", "bge-m3").strip().lower() in {
                "text-only",
                "bm25",
                "lexical",
            }:
                embedding = embedding_result(
                    target_count=len(page_ids),
                    skipped_count=len(page_ids),
                )
            else:
                embedding = build_wiki_embeddings(page_ids)
            if embedding["failed_count"]:
                raise RuntimeError(
                    f"post_ingest embedding failed: {embedding['failed_count']}"
                )
            state["embedding"] = embedding
            database.checkpoint_pipeline_run(run_id, state)

        evaluations = _run_post_ingest_wiki_quality_evaluation(command, state)
        quality_required = bool(command.get("quality_required", True))
        generation_ready = (
            not quality_required
            or command.get("generation_evaluation_status") == "passed"
        )
        wiki_ready = (
            not quality_required
            or (
                len(evaluations) == POST_INGEST_AUDIT_LIMIT
                and all(item["passed"] for item in evaluations)
            )
        )
        quality_status = (
            "ready" if generation_ready and wiki_ready else "needs_review"
        )
        result = {
            "quality_status": quality_status,
            "maintenance_summary": state["cluster_artifact"].get(
                "maintenance_summary",
                {},
            ),
            "embedding": state["embedding"],
            "generation_evaluation_status": command.get(
                "generation_evaluation_status"
            ),
            "wiki_quality_evaluations": evaluations,
        }
        database.update_pipeline_run_post_ingest(
            str(command["ingest_run_id"]),
            {
                "run_id": run_id,
                "status": quality_status,
                "result": result,
                "error": None,
            },
        )
        database.finish_pipeline_run(run_id, {**state, "task_result": result})
        return result
    except Exception as exc:
        final_attempt = attempt >= POST_INGEST_MAX_ATTEMPTS
        database.update_pipeline_run_post_ingest(
            str(command["ingest_run_id"]),
            {
                "run_id": run_id,
                "status": "needs_review" if final_attempt else "retrying",
                "attempt": attempt,
                "error": str(exc)[:1000],
            },
        )
        if final_attempt:
            database.fail_pipeline_run(run_id, str(exc))
        raise


def _handle(
    command: dict[str, Any],
    event_publisher: QueryEventPublisherPort | None = None,
) -> dict[str, Any]:
    ensure_task_active()
    kind = str(command.get("kind") or "")
    if kind == "query":
        return _handle_query(command, event_publisher)
    if kind == "agent":
        return _handle_agent(command, event_publisher)
    if kind == "lint":
        return _handle_lint(command)
    if kind == "post_ingest":
        for attempt in range(POST_INGEST_MAX_ATTEMPTS):
            try:
                ensure_task_active()
                return _handle_post_ingest(command)
            except Exception:
                if (
                    attempt + 1 == POST_INGEST_MAX_ATTEMPTS
                    or _failure_is_durable(command)
                ):
                    raise
    if kind in {"restore_ingest", "restore_lint"}:
        return _handle_restore(command)
    raise ValueError(f"unsupported AI command kind: {kind}")



def _handle_controlled(command: dict[str, Any], event_publisher: QueryEventPublisherPort | None = None) -> dict[str, Any]:
    from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
    if command["kind"] != "agent":
        return journal.execute(command, lambda: _handle(command, event_publisher))

    def active() -> bool:
        with database.connect_ai() as conn:
            row = conn.execute("SELECT status FROM agent_runs WHERE id = %s", (command["run_id"],)).fetchone()
        return row is None or row["status"] not in journal.STOPPING

    with PostgresAgentJobRepository().execution_lock(command["run_id"]), task_cancellation_scope(active):
        result = _handle(command, event_publisher)
        ensure_task_active()
        return result


def _event(
    command: dict[str, Any],
    status: str,
    payload: Any = None,
    error: str | None = None,
    *,
    with_request: bool = True,
) -> dict[str, Any]:
    """`with_request=False`는 소비 측이 원본 command를 보지 않는 이벤트에 쓴다."""
    run_id = str(command.get("run_id") or "")
    kind = str(command.get("kind") or "unknown")
    event: dict[str, Any] = {
        "event_id": f"{kind}:{run_id}:{status}",
        "run_id": run_id,
        "kind": kind,
        "status": status,
        "workspace_id": command.get("workspace_id"),
        "user_id": command.get("user_id"),
        "operation_id": command.get("operation_id"),
    }
    if with_request:
        event["request"] = without_top_level_secrets(command)
    event["payload"] = payload
    event["error"] = error
    return event


def _progress_event(
    command: dict[str, Any],
    *,
    sequence: int,
    stage: str,
    message: str,
    data: dict[str, object] | None,
) -> dict[str, Any]:
    # 소비 측은 진행 이벤트에서 run_id·event_id·status·payload만 읽는다.
    # command 전체(질문·대화 이력)를 단계마다 다시 실어 보내지 않는다.
    event = _event(
        command,
        "progress",
        payload={"stage": stage, "message": message, "data": data or {}},
        with_request=False,
    )
    event["event_id"] = f"query:{event['run_id']}:progress:{sequence}:{stage}"
    return event


class KafkaQueryEventPublisher(QueryEventPublisherPort):
    """동기 query pipeline의 진행 이벤트를 worker event loop의 Kafka producer로 전달한다."""

    # 진행 이벤트는 유실을 허용한다. Kafka가 응답하지 않을 때 답변 생성 스레드를
    # 무기한 붙잡지 않도록 제한을 둔다. 호출자(publish_query_event)가 예외를 삼킨다.
    SEND_TIMEOUT_SECONDS = 5

    def __init__(
        self,
        producer: AIOKafkaProducer,
        loop: asyncio.AbstractEventLoop,
        command: dict[str, Any],
    ) -> None:
        self._producer = producer
        self._loop = loop
        self._command = command
        self._sequences = count(1)

    def publish(
        self,
        stage: str,
        message: str,
        data: dict[str, object] | None = None,
    ) -> None:
        event = _progress_event(
            self._command,
            sequence=next(self._sequences),
            stage=stage,
            message=message,
            data=data,
        )
        future = asyncio.run_coroutine_threadsafe(
            self._producer.send_and_wait(
                RESULT_TOPIC,
                event,
                key=event["run_id"].encode("utf-8"),
            ),
            self._loop,
        )
        try:
            future.result(self.SEND_TIMEOUT_SECONDS)
        except TimeoutError:
            future.cancel()
            raise


def _failure_is_durable(command: dict[str, Any]) -> bool:
    kind = str(command.get("kind") or "")
    run_id = str(command.get("run_id") or "")
    if kind not in {
        "query",
        "agent",
        "lint",
        "post_ingest",
        "restore_ingest",
        "restore_lint",
    }:
        return True
    try:
        if kind == "agent":
            with database.connect_ai() as conn:
                row = conn.execute(
                    "SELECT status FROM agent_runs WHERE id = %s",
                    (run_id,),
                ).fetchone()
            return row is not None and row["status"] in {
                "completed", "failed", "cancel_requested", "rolling_back", "rollback_failed", "cancelled",
            }
        from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
        with journal.connect() as conn:
            controlled = conn.execute("SELECT status FROM ai_task_runs WHERE id = %s", (run_id,)).fetchone()
        return controlled is not None and controlled["status"] in {*journal.STOPPING, "failed", "completed"}
    except Exception:
        return False


async def consume() -> None:
    database.ensure_ai_schema()
    if COMMAND_TOPIC.endswith("agent.command"):
        database.verify_agent_schema()
    if COMMAND_TOPIC.endswith("maintenance.command"):
        get_wiki_embedding_job().start("pending-recovery", [])
    consumer = AIOKafkaConsumer(
        COMMAND_TOPIC,
        bootstrap_servers=BOOTSTRAP_SERVERS,
        group_id=GROUP_ID,
        enable_auto_commit=False,
        max_poll_records=1,
        max_poll_interval_ms=MAX_POLL_INTERVAL_MS,
        auto_offset_reset="earliest",
        value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=BOOTSTRAP_SERVERS,
        enable_idempotence=True,
        value_serializer=lambda value: json.dumps(value, ensure_ascii=False).encode("utf-8"),
    )
    await consumer.start()
    await producer.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("[worker 기동] topic=%s group=%s", COMMAND_TOPIC, GROUP_ID)
    try:
        while not stop.is_set():
            from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
            await asyncio.to_thread(journal.rollback_pending)
            batch = await consumer.getmany(timeout_ms=1000, max_records=1)
            for _tp, messages in batch.items():
                for message in messages:
                    command = message.value
                    try:
                        # agent도 질의 갈래(chat_answer)로 갈리면 같은 진행 이벤트를 낸다.
                        if command.get("kind") in {"query", "agent"}:
                            event_publisher = KafkaQueryEventPublisher(producer, loop, command)
                            result = await asyncio.to_thread(_handle_controlled, command, event_publisher)
                        else:
                            result = await asyncio.to_thread(_handle_controlled, command)
                        event = _event(
                            command,
                            "succeeded",
                            payload=result,
                            with_request=command.get("kind") != "post_ingest",
                        )
                    except Exception as exc:
                        if command.get("kind") == "agent":
                            error = _agent_failure_code(exc)
                            logger.error(
                                "[AI command 실패] kind=agent run_id=%s error_code=%s error_type=%s",
                                command.get("run_id"),
                                error,
                                type(exc).__name__,
                            )
                        else:
                            error = str(exc)[:1000]
                            logger.exception(
                                "[AI command 실패] kind=%s run_id=%s",
                                command.get("kind"),
                                command.get("run_id"),
                            )
                        if not _failure_is_durable(command):
                            raise
                        event = _event(
                            command,
                            "failed",
                            error=error,
                            with_request=command.get("kind") != "post_ingest",
                        )
                    await producer.send_and_wait(RESULT_TOPIC, event, key=event["run_id"].encode("utf-8"))
                    await consumer.commit()
    finally:
        await producer.stop()
        await consumer.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(consume())


if __name__ == "__main__":
    main()
