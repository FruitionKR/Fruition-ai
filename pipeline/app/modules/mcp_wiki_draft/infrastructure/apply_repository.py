from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.core.pipeline_control import task_run_id
from app.modules.mcp_wiki_draft.application import McpWikiDraftError
from app.modules.mcp_wiki_draft.apply_application import ApplyClaim
from app.modules.mcp_wiki_draft.calculation import PreviewCalculationError, publish_ingest
from app.modules.mcp_wiki_draft.infrastructure.postgres_repository import connect
from app.modules.mcp_wiki_draft.interfaces.http.apply_schemas import ApplyWikiDraftRequest
from app.modules.mcp_wiki_draft.lint_calculation import calculate_lint_preview
from app.modules.wiki_ingestion.application.ports import WikiEmbeddingJobPort
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import (
    finalize_ingest_publication,
    persist_lint_operation_result,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_output_persistence import (
    lock_concept_persistence,
)
from app.modules.wiki_ingestion.infrastructure.wiki_maintenance import (
    _embedding_page_ids,
    finalize_lint_publication,
)
from app.modules.wiki_ingestion.infrastructure.wiki_publication import (
    object_key,
    prepare_publication,
    write_publication_text,
)
from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import (
    concept_write_lock,
    invalidate_concept_index,
)


logger = logging.getLogger(__name__)


_TERMINAL = {"PUBLISHED", "NO_CHANGE", "CONFLICTED", "HALTED"}


class _PublicationConflict(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class PostgresMcpWikiApplyRepository:
    def __init__(self, embedding_job: WikiEmbeddingJobPort | None = None) -> None:
        self._embedding_job = embedding_job

    @contextmanager
    def execution_lock(self, approval_id: str, operation_id: str) -> Iterator[None]:
        keys = sorted((f"mcp-wiki-approval:{approval_id}", f"mcp-wiki-operation:{operation_id}"))
        with connect() as conn:
            for key in keys:
                conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
            try:
                yield
            finally:
                for key in reversed(keys):
                    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))

    def claim(self, payload: ApplyWikiDraftRequest, request_hash: str) -> ApplyClaim:
        actor = payload.actor
        with connect() as conn:
            _reject_purged(conn, actor.workspace_id, actor.user_id)
            bound = conn.execute(
                "SELECT * FROM mcp_wiki_publication_results WHERE approval_id=%s OR operation_id=%s FOR UPDATE",
                (payload.approval_id, payload.operation_id),
            ).fetchall()
            if bound:
                if len(bound) != 1 or not _same_binding(bound[0], payload, request_hash):
                    raise McpWikiDraftError(409, "IDEMPOTENCY_CONFLICT", "Approval or operation is bound to different apply intent.")
                if bound[0]["state"] in _TERMINAL:
                    return ApplyClaim(run_id="", lookup={}, stored_result=_result(bound[0]))
                conn.execute(
                    "UPDATE mcp_wiki_publication_results SET state='APPLYING',error_code=NULL,updated_at=now() WHERE approval_id=%s",
                    (payload.approval_id,),
                )
            draft = _load_draft(conn, payload, lock=True)
            if not bound and conn.execute(
                "SELECT 1 FROM mcp_wiki_publication_results WHERE draft_id=%s AND draft_version=%s FOR UPDATE",
                (payload.draft_id, payload.draft_version),
            ).fetchone() is not None:
                raise McpWikiDraftError(
                    409, "IDEMPOTENCY_CONFLICT", "Draft version is bound to a different apply intent."
                )
            task_id = str(draft["run_id"] or draft["draft_id"])
            task = conn.execute("SELECT status,command FROM ai_task_runs WHERE id=%s FOR UPDATE", (task_id,)).fetchone()
            if task is None or task["status"] != "running":
                raise McpWikiDraftError(409, "CANCELLED", "Draft task no longer accepts publication.")
            if not bound:
                conn.execute(
                    """
                    INSERT INTO mcp_wiki_publication_results(
                        approval_id,operation_id,draft_id,draft_version,apply_request_hash,workspace_id,user_id,
                        grant_id,client_id,policy_generation,draft_hash,public_state_hash,state
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'APPLYING')
                    """,
                    (
                        payload.approval_id, payload.operation_id, payload.draft_id, payload.draft_version,
                        request_hash, actor.workspace_id, actor.user_id, actor.grant_id, actor.client_id,
                        actor.policy_generation, payload.draft_hash, payload.public_state_hash,
                    ),
                )
            _update_lifecycle(conn, payload.draft_id, "APPLYING")
            command = dict(task["command"] or {})
            command.update({"approval_id": payload.approval_id, "operation_id": payload.operation_id, "draft_id": payload.draft_id})
            command_hash = hashlib.sha256(_canonical(command)).hexdigest()
            conn.execute(
                "UPDATE ai_task_runs SET command=%s,command_hash=%s,updated_at=now() WHERE id=%s",
                (Jsonb(command), command_hash, task_id),
            )
            return ApplyClaim(
                run_id=task_id,
                lookup={
                    "schema_version": "mcp.wiki-approval-lookup.v1",
                    "approval_id": payload.approval_id,
                    "operation_id": payload.operation_id,
                    "draft_id": payload.draft_id,
                    "draft_version": payload.draft_version,
                    "draft_hash": payload.draft_hash,
                    "public_state_hash": payload.public_state_hash,
                    "workspace_id": actor.workspace_id,
                    "user_id": actor.user_id,
                    "grant_id": actor.grant_id,
                    "client_id": actor.client_id,
                    "policy_generation": actor.policy_generation,
                    "source_snapshots": list(draft["source_snapshots"]),
                },
            )

    def finish_without_publication(
        self,
        claim: ApplyClaim,
        payload: ApplyWikiDraftRequest,
        state: str,
        error_code: str,
    ) -> dict[str, Any]:
        with connect() as conn:
            row = conn.execute(
                """
                UPDATE mcp_wiki_publication_results
                SET state=%s,changed=NULL,event_id=NULL,published_reference_hash=NULL,committed_at=NULL,
                    error_code=%s,updated_at=now()
                WHERE approval_id=%s AND operation_id=%s AND state IN ('APPLYING','UNKNOWN')
                RETURNING *
                """,
                (state, error_code, payload.approval_id, payload.operation_id),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM mcp_wiki_publication_results WHERE approval_id=%s AND operation_id=%s",
                    (payload.approval_id, payload.operation_id),
                ).fetchone()
                if row is None:
                    raise McpWikiDraftError(404, "NOT_FOUND", "Publication result not found.")
                return _result(row)
            _update_lifecycle(conn, payload.draft_id, state)
            if state != "UNKNOWN":
                conn.execute(
                    "UPDATE ai_task_runs SET status='completed',result=%s,error_code=NULL,updated_at=now() "
                    "WHERE id=%s AND status='running'",
                    (Jsonb(_result(row)), claim.run_id),
                )
            return _result(row)

    def publish(self, claim: ApplyClaim, payload: ApplyWikiDraftRequest) -> dict[str, Any]:
        actor = payload.actor
        token = task_run_id.set(claim.run_id)
        page_ids: list[str] = []
        try:
            with concept_write_lock(actor.workspace_id, claim.run_id):
                with connect() as conn:
                    _reject_purged(conn, actor.workspace_id, actor.user_id)
                    # Keep cancellation ordered through commit while allowing immutable-object journal FK checks.
                    task = conn.execute("SELECT status FROM ai_task_runs WHERE id=%s FOR NO KEY UPDATE", (claim.run_id,)).fetchone()
                    if task is None or task["status"] != "running":
                        row = conn.execute(
                            """
                            UPDATE mcp_wiki_publication_results
                            SET state='HALTED',error_code='CANCELLED',updated_at=now()
                            WHERE approval_id=%s AND operation_id=%s AND state='APPLYING'
                            RETURNING *
                            """,
                            (payload.approval_id, payload.operation_id),
                        ).fetchone()
                        if row is not None:
                            _update_lifecycle(conn, payload.draft_id, "HALTED")
                        else:
                            row = conn.execute(
                                "SELECT * FROM mcp_wiki_publication_results WHERE approval_id=%s AND operation_id=%s",
                                (payload.approval_id, payload.operation_id),
                            ).fetchone()
                        return _result(row)
                    draft = _load_draft(conn, payload, lock=True)
                    lock_concept_persistence(conn, actor.workspace_id)
                    kind = str(draft["draft_kind"])
                    try:
                        # 승인한 미리보기와 다시 계산한 결과가 다르면 savepoint를 되돌려 공개 상태를 그대로 둔다.
                        with conn.transaction():
                            if kind == "INGEST":
                                event, page_ids = _publish_ingest(conn, payload, draft)
                            else:
                                event, page_ids = self._publish_lint(conn, payload, draft)
                    except _PublicationConflict as conflict:
                        page_ids = []
                        return self._conflict(conn, claim, payload, conflict.code)
                    state = str(event["outcome"])
                    changed = state == "PUBLISHED"
                    event_id = str(uuid4())
                    committed_at = conn.execute("SELECT clock_timestamp() AS value").fetchone()["value"]
                    reference_hash = str(event["published_reference_hash"])
                    event = {
                        "event_id": event_id,
                        "event_type": "mcp.wiki.publication.v1",
                        "approval_id": payload.approval_id,
                        "operation_id": payload.operation_id,
                        "draft_id": payload.draft_id,
                        "workspace_id": actor.workspace_id,
                        "user_id": actor.user_id,
                        "grant_id": actor.grant_id,
                        "client_id": actor.client_id,
                        "kind": kind,
                        "outcome": state,
                        "changed": changed,
                        "published_reference_hash": reference_hash,
                        "committed_at": _instant(committed_at),
                    }
                    row = conn.execute(
                        """
                        UPDATE mcp_wiki_publication_results
                        SET state=%s,changed=%s,event_id=%s,published_reference_hash=%s,committed_at=%s,
                            error_code=NULL,updated_at=now()
                        WHERE approval_id=%s AND operation_id=%s AND state='APPLYING'
                        RETURNING *
                        """,
                        (
                            state, changed, event_id, reference_hash, committed_at,
                            payload.approval_id, payload.operation_id,
                        ),
                    ).fetchone()
                    if row is None:
                        raise McpWikiDraftError(409, "IDEMPOTENCY_CONFLICT", "Apply result changed during publication.")
                    conn.execute(
                        "INSERT INTO mcp_wiki_publication_outbox(event_id,approval_id,operation_id,payload) VALUES (%s,%s,%s,%s)",
                        (event_id, payload.approval_id, payload.operation_id, Jsonb(event)),
                    )
                    _update_lifecycle(conn, payload.draft_id, state)
                    if kind == "INGEST":
                        # Edit events before or after this conditional update both leave a newer source stale.
                        source_hash = str(draft["source_content_hash"]).removeprefix("sha256:")
                        conn.execute(
                            """
                            UPDATE document_derived_state
                            SET ingested_hash=%s,last_ingested_at=now(),updated_at=now()
                            WHERE document_id=%s AND workspace_id=%s AND last_edit_hash=%s
                            """,
                            (source_hash, draft["document_id"], actor.workspace_id, source_hash),
                        )
                    conn.execute(
                        "UPDATE ai_task_runs SET status='completed',result=%s,error_code=NULL,updated_at=now() WHERE id=%s",
                        (Jsonb(_result(row)), claim.run_id),
                    )
                try:
                    invalidate_concept_index(actor.workspace_id)
                except Exception:
                    pass
        finally:
            task_run_id.reset(token)
        # 웹 편입·lint와 같이 커밋 뒤 임베딩을 시작한다. 실패해도 발행은 끝났고 복구 작업이 다시 고른다.
        if page_ids and self._embedding_job is not None:
            try:
                self._embedding_job.start(payload.operation_id, page_ids)
            except Exception:
                logger.warning("MCP wiki embedding start failed after publication commit", exc_info=True)
        return _result(row)

    def _publish_lint(
        self, conn, payload: ApplyWikiDraftRequest, draft: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        actor = payload.actor
        try:
            calculation = _recalculate_lint(conn, draft)
        except PreviewCalculationError:
            raise _PublicationConflict("PUBLIC_STATE_CHANGED") from None
        _require_approved(calculation["preview"], payload, draft)
        prepare_publication(payload.operation_id, actor.workspace_id, actor.user_id, "LINT", database_connect=connect)
        plan = calculation["publication_plan"]
        deleted = list(plan["deleted_page_ids"])
        result = {
            "workspace_id": actor.workspace_id,
            "deleted_pages": deleted,
            "_lint_page_changes": plan["page_changes"],
            "_entry_state": {
                "pages": [
                    {**page, "markdown_key": object_key(actor.workspace_id, page["page_id"], payload.operation_id)}
                    for page in plan["state_pages"]
                ],
                "link_changes": plan["link_changes"],
                # 원래 작업 id로 다른 페이지에 이은 기록이다. lint 복원이 이 표시로 함께 뺀다.
                "linked": [{**item, "linked_by_operation_id": payload.operation_id} for item in plan["linked"]],
                "deleted_pages": deleted,
            },
        }
        artifacts = persist_lint_operation_result(
            actor.workspace_id, payload.operation_id, result,
            connection=conn, artifact_writer=_publication_writer(payload.operation_id),
        )
        event = finalize_lint_publication(
            conn,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            operation_id=payload.operation_id,
            result=result,
            operation_artifacts=artifacts,
            reserve_embeddings=self._embedding_job is not None,
        )
        return event, _embedding_page_ids(result)

    def _conflict(
        self, conn, claim: ApplyClaim, payload: ApplyWikiDraftRequest, error_code: str
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            UPDATE mcp_wiki_publication_results
            SET state='CONFLICTED',error_code=%s,updated_at=now()
            WHERE approval_id=%s AND operation_id=%s AND state='APPLYING'
            RETURNING *
            """,
            (error_code, payload.approval_id, payload.operation_id),
        ).fetchone()
        _update_lifecycle(conn, payload.draft_id, "CONFLICTED")
        conn.execute(
            "UPDATE ai_task_runs SET status='completed',result=%s,error_code=NULL,updated_at=now() "
            "WHERE id=%s AND status='running'",
            (Jsonb(_result(row)), claim.run_id),
        )
        return _result(row)

    def result(
        self, approval_id: str, operation_id: str, workspace_id: str, user_id: str
    ) -> dict[str, Any]:
        with connect() as conn:
            row = conn.execute(
                """
                SELECT result.* FROM mcp_wiki_publication_results result
                WHERE approval_id=%s AND operation_id=%s AND workspace_id=%s AND user_id=%s
                  AND NOT EXISTS (
                    SELECT 1 FROM ai_purged_scopes
                    WHERE (scope_type='workspace' AND scope_id=result.workspace_id)
                       OR (scope_type='user' AND scope_id=result.user_id)
                  )
                """,
                (approval_id, operation_id, workspace_id, user_id),
            ).fetchone()
        return _not_found(approval_id, operation_id) if row is None else _result(row)


def _load_draft(conn, payload: ApplyWikiDraftRequest, *, lock: bool) -> dict[str, Any]:
    actor = payload.actor
    suffix = " FOR UPDATE" if lock else ""
    stored = conn.execute(
        "SELECT * FROM mcp_wiki_drafts WHERE draft_id=%s AND draft_version=%s "
        "AND workspace_id=%s AND user_id=%s" + suffix,
        (payload.draft_id, payload.draft_version, actor.workspace_id, actor.user_id),
    ).fetchone()
    if stored is None:
        raise McpWikiDraftError(404, "NOT_FOUND", "Draft not found.")
    row = dict(stored)
    if row["draft_kind"] == "INGEST":
        run = conn.execute(
            "SELECT * FROM mcp_wiki_ingest_runs WHERE run_id=%s" + suffix,
            (str(row["run_id"]),),
        ).fetchone()
        if run is None:
            raise McpWikiDraftError(404, "NOT_FOUND", "Draft run not found.")
        for key in (
            "snapshot_id", "document_id", "grant_id", "client_id", "policy_generation",
            "origin", "spec_version", "source_version", "source_content_hash",
            "source_display_name", "expires_at", "source_block_count",
        ):
            row[key] = run[key]
        row["lifecycle_state"] = run["state"]
    else:
        row.update({
            "snapshot_id": None,
            "document_id": None,
            "source_version": None,
            "source_content_hash": None,
            "source_block_count": None,
            "lifecycle_state": row["state"],
        })
    if (
        str(row["grant_id"]) != actor.grant_id
        or row["client_id"] != actor.client_id
        or row["policy_generation"] != actor.policy_generation
        or row["origin"] != actor.origin
        or row["spec_version"] != actor.spec_version
        or row["draft_hash"] != payload.draft_hash
        or row["public_state_hash"] != payload.public_state_hash
    ):
        raise McpWikiDraftError(409, "IDEMPOTENCY_CONFLICT", "Apply identity does not match the frozen draft.")
    if row["lifecycle_state"] not in {"AWAITING_WEB_APPROVAL", "APPLYING", "UNKNOWN"}:
        raise McpWikiDraftError(409, str(row["lifecycle_state"]), "Draft is not available for apply.")
    if row["lifecycle_state"] == "AWAITING_WEB_APPROVAL":
        database_now = conn.execute("SELECT now() AS value").fetchone()["value"]
        if row["expires_at"] <= database_now:
            raise McpWikiDraftError(410, "EXPIRED", "Draft expired before approval was claimed.")
    return row


def _publication_writer(operation_id: str):
    return lambda key, text, content_type: write_publication_text(
        operation_id,
        key.split("/pages/", 1)[1].split("/", 1)[0],
        key,
        text,
        content_type,
        database_connect=connect,
    )


def _require_approved(fresh: dict[str, Any], payload: ApplyWikiDraftRequest, draft: dict[str, Any]) -> None:
    if list(fresh["source_snapshots"]) != list(draft["source_snapshots"]):
        raise _PublicationConflict("SOURCE_CHANGED")
    if (
        fresh["draft_hash"] != payload.draft_hash
        or fresh["public_state_hash"] != payload.public_state_hash
        or list(fresh["target_pages"]) != list(draft["target_pages"])
        or list(fresh["changes"]) != list(draft["changes"])
    ):
        raise _PublicationConflict("PUBLIC_STATE_CHANGED")


def _publish_ingest(
    conn, payload: ApplyWikiDraftRequest, draft: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """웹 편입 반영을 그대로 실행한 뒤, 같은 계산의 미리보기 해시를 승인값과 맞춘다. 다르면 호출자가 되돌린다."""
    actor = payload.actor
    source_blocks = [dict(row) for row in conn.execute(
        "SELECT block_id,ordinal,text,content_hash FROM mcp_wiki_source_blocks "
        "WHERE snapshot_id=%s ORDER BY ordinal",
        (draft["snapshot_id"],),
    ).fetchall()]
    prepare_publication(payload.operation_id, actor.workspace_id, actor.user_id, "INGEST", database_connect=connect)
    try:
        calculation, page_ids, manifest = publish_ingest(
            conn,
            run=draft,
            submissions=list(draft["normalized_submissions"]),
            source_blocks=source_blocks,
            operation_id=payload.operation_id,
            artifact_writer=_publication_writer(payload.operation_id),
        )
    except PreviewCalculationError:
        raise _PublicationConflict("PUBLIC_STATE_CHANGED") from None
    _require_approved(calculation["preview"], payload, draft)
    event = finalize_ingest_publication(
        conn,
        document_id=str(draft["document_id"]),
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        manifest=manifest,
        page_ids=page_ids,
    )
    return event, page_ids


def _recalculate_lint(conn, draft: dict[str, Any]) -> dict[str, Any]:
    structural = sum(
        1 for item in draft["changes"]
        if item["kind"] in {"ENTRY", "LINK", "REFERENCE", "PAGE_DELETE"}
    )
    deletes = sum(1 for item in draft["changes"] if item["kind"] == "PAGE_DELETE")
    return calculate_lint_preview(
        conn,
        draft_id=str(draft["draft_id"]),
        workspace_id=str(draft["workspace_id"]),
        target_snapshot=list(draft["target_pages"]),
        source_snapshots=list(draft["source_snapshots"]),
        operations=list(draft["normalized_submissions"]),
        policy_version=str(draft["calculation_inputs"]["policy_version"]),
        max_structural_changes=structural,
        max_deletes=deletes,
        server_effects=draft["calculation_inputs"].get("server_effects", {}),
    )


def _update_lifecycle(conn, draft_id: str, state: str) -> None:
    lint = conn.execute(
        "UPDATE mcp_wiki_drafts SET state=%s,updated_at=now() "
        "WHERE draft_id=%s AND draft_kind='LINT' RETURNING draft_id",
        (state, draft_id),
    ).fetchone()
    if lint is None:
        conn.execute(
            "UPDATE mcp_wiki_ingest_runs SET state=%s,updated_at=now() WHERE draft_id=%s",
            (state, draft_id),
        )


def _same_binding(row: dict[str, Any], payload: ApplyWikiDraftRequest, request_hash: str) -> bool:
    actor = payload.actor
    return (
        str(row["approval_id"]) == payload.approval_id
        and row["operation_id"] == payload.operation_id
        and str(row["draft_id"]) == payload.draft_id
        and row["draft_version"] == payload.draft_version
        and row["apply_request_hash"] == request_hash
        and row["workspace_id"] == actor.workspace_id
        and row["user_id"] == actor.user_id
        and str(row["grant_id"]) == actor.grant_id
        and row["client_id"] == actor.client_id
        and row["policy_generation"] == actor.policy_generation
        and row["draft_hash"] == payload.draft_hash
        and row["public_state_hash"] == payload.public_state_hash
    )


def _reject_purged(conn, workspace_id: str, user_id: str) -> None:
    if conn.execute(
        "SELECT 1 FROM ai_purged_scopes WHERE (scope_type='workspace' AND scope_id=%s) OR (scope_type='user' AND scope_id=%s)",
        (workspace_id, user_id),
    ).fetchone():
        raise McpWikiDraftError(410, "PURGED", "Publication scope was purged.")


def _result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "mcp.wiki-result.v1",
        "approval_id": str(row["approval_id"]),
        "operation_id": row["operation_id"],
        "draft_id": str(row["draft_id"]),
        "state": row["state"],
        "changed": row["changed"],
        "event_id": str(row["event_id"]) if row["event_id"] else None,
        "published_reference_hash": row["published_reference_hash"],
        "committed_at": _instant(row["committed_at"]) if row["committed_at"] else None,
        "error_code": row["error_code"],
    }


def _not_found(approval_id: str, operation_id: str) -> dict[str, Any]:
    return {
        "schema_version": "mcp.wiki-result.v1",
        "approval_id": approval_id,
        "operation_id": operation_id,
        "draft_id": None,
        "state": "NOT_FOUND",
        "changed": None,
        "event_id": None,
        "published_reference_hash": None,
        "committed_at": None,
        "error_code": None,
    }


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()


def _instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def pending_mcp_publication_events(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT outbox.event_id,outbox.payload,result.workspace_id,result.user_id
            FROM mcp_wiki_publication_outbox outbox
            JOIN mcp_wiki_publication_results result USING(approval_id)
            WHERE outbox.state='pending' AND outbox.next_attempt_at<=now()
              AND NOT EXISTS (
                SELECT 1 FROM ai_purged_scopes
                WHERE (scope_type='workspace' AND scope_id=result.workspace_id)
                   OR (scope_type='user' AND scope_id=result.user_id)
              )
            ORDER BY outbox.created_at LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _send_publication_receipt(payload: dict[str, Any]) -> None:
    base_url = os.environ.get("DOCUMENT_INTERNAL_BASE_URL", "").rstrip("/")
    token = os.environ.get("AGENT_INTERNAL_TOKEN", "").strip()
    try:
        timeout = int(os.environ.get("MCP_WIKI_PUBLICATION_RECEIPT_TIMEOUT_SECONDS", ""))
        if not base_url or not token or timeout <= 0:
            raise ValueError
    except ValueError:
        raise RuntimeError("MCP wiki publication receipt transport is not configured.") from None
    request = Request(
        base_url + "/internal/mcp/wiki/publications/receipt",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Agent-Service-Token": token},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read(8_193)
            if not 200 <= response.status < 300:
                raise RuntimeError(f"publication receipt returned HTTP {response.status}")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("publication receipt delivery failed") from exc


async def relay_pending_mcp_publications(limit: int = 20) -> int:
    sent = 0
    for row in pending_mcp_publication_events(limit):
        event_id = str(row["event_id"])
        payload = dict(row["payload"])
        try:
            with connect() as conn:
                conn.execute(
                    "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
                    ("ai-workspace-write:" + str(row["workspace_id"]),),
                )
                conn.execute(
                    "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
                    ("ai-user-write:" + str(row["user_id"]),),
                )
                _reject_purged(conn, str(row["workspace_id"]), str(row["user_id"]))
                await asyncio.to_thread(_send_publication_receipt, payload)
                conn.execute(
                    """
                    UPDATE mcp_wiki_publication_outbox
                    SET state='sent',attempts=attempts+1,sent_at=now()
                    WHERE event_id=%s AND state='pending'
                    """,
                    (event_id,),
                )
        except Exception:
            with connect() as conn:
                conn.execute(
                    "UPDATE mcp_wiki_publication_outbox SET attempts=attempts+1,next_attempt_at=now()+interval '5 seconds' "
                    "WHERE event_id=%s AND state='pending'",
                    (event_id,),
                )
            raise
        sent += 1
    return sent
