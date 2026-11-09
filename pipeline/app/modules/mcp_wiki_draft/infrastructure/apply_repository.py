from __future__ import annotations

import asyncio
import hashlib
import json
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
from app.modules.mcp_wiki_draft.calculation import PreviewCalculationError, calculate_ingest_preview
from app.modules.mcp_wiki_draft.infrastructure.postgres_repository import connect
from app.modules.mcp_wiki_draft.interfaces.http.apply_schemas import ApplyWikiDraftRequest
from app.modules.mcp_wiki_draft.lint_calculation import calculate_lint_preview
from app.modules.wiki_ingestion.infrastructure.pending_contributions import record_pending_deltas
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import (
    _apply_restored_wiki_state,
    _cleanup_deleted_wiki_pages,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_output_persistence import (
    lock_concept_persistence,
)
from app.modules.wiki_ingestion.infrastructure.wiki_publication import (
    finalize_publication,
    object_key,
    prepare_publication,
    publication_id,
    write_publication_text,
)
from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import (
    concept_write_lock,
    invalidate_concept_index,
)


_TERMINAL = {"PUBLISHED", "NO_CHANGE", "CONFLICTED", "HALTED"}


class PostgresMcpWikiApplyRepository:
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
                    try:
                        calculation, source_blocks = _recalculate(conn, draft)
                    except PreviewCalculationError:
                        return self._conflict(conn, claim, payload, "PUBLIC_STATE_CHANGED")
                    fresh = calculation["preview"]
                    if list(fresh["source_snapshots"]) != list(draft["source_snapshots"]):
                        return self._conflict(conn, claim, payload, "SOURCE_CHANGED")
                    if (
                        fresh["draft_hash"] != payload.draft_hash
                        or fresh["public_state_hash"] != payload.public_state_hash
                        or list(fresh["target_pages"]) != list(draft["target_pages"])
                        or list(fresh["changes"]) != list(draft["changes"])
                    ):
                        return self._conflict(conn, claim, payload, "PUBLIC_STATE_CHANGED")

                    kind = str(draft["draft_kind"])
                    plan = calculation["publication_plan"]
                    changed = _plan_has_effect(conn, actor.workspace_id, draft, plan, source_blocks)
                    prepare_publication(
                        payload.operation_id, actor.workspace_id, actor.user_id, kind, database_connect=connect
                    )
                    artifacts, ledger = _write_artifacts(
                        payload.operation_id, actor.workspace_id, payload.draft_id, kind, plan
                    ) if changed else ([], [])
                    if changed:
                        _apply_plan(
                            conn, actor, draft, plan, source_blocks, artifacts, ledger, payload.operation_id
                        )
                    normalized_result = {
                        "operation_id": payload.operation_id,
                        "operation_type": kind.lower(),
                        "status": "succeeded",
                        "workspace_id": actor.workspace_id,
                        "user_id": actor.user_id,
                        "target_document_id": str(draft["document_id"]) if kind == "INGEST" else None,
                        "summary": f"Wiki {kind.lower()} completed.",
                        "changed_pages": artifacts,
                        "failed_pages": [],
                        "deleted_pages": list(plan["deleted_page_ids"]) if changed else [],
                        "link_changes": None,
                        "failed_actions": None,
                        "ledger_contributions": ledger,
                    }
                    legacy_event = finalize_publication(
                        conn,
                        operation_id=payload.operation_id,
                        workspace_id=actor.workspace_id,
                        user_id=actor.user_id,
                        kind=kind,
                        outcome="PUBLISHED" if changed else "NO_CHANGE",
                        result=normalized_result,
                    )
                    state = "PUBLISHED" if changed else "NO_CHANGE"
                    event_id = str(uuid4())
                    committed_at = conn.execute("SELECT clock_timestamp() AS value").fetchone()["value"]
                    reference_hash = str(legacy_event["published_reference_hash"])
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
                return _result(row)
        finally:
            task_run_id.reset(token)

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
            "expires_at", "source_block_count",
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


def _recalculate(conn, draft: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if draft["draft_kind"] == "INGEST":
        source_blocks = [dict(row) for row in conn.execute(
            "SELECT block_id,ordinal,text,content_hash FROM mcp_wiki_source_blocks "
            "WHERE snapshot_id=%s ORDER BY ordinal",
            (draft["snapshot_id"],),
        ).fetchall()]
        return calculate_ingest_preview(
            conn,
            run=draft,
            submissions=list(draft["normalized_submissions"]),
            source_blocks=source_blocks,
        ), source_blocks
    structural = sum(
        1 for item in draft["changes"]
        if item["kind"] in {"ENTRY", "LINK", "REFERENCE", "PAGE_DELETE"}
    )
    deletes = sum(1 for item in draft["changes"] if item["kind"] == "PAGE_DELETE")
    calculation = calculate_lint_preview(
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
    return calculation, []


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


def _plan_pages(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return ([plan["source_page"]] if plan["source_page"] is not None else []) + list(plan["pages"])


def _write_artifacts(
    operation_id: str,
    workspace_id: str,
    draft_id: str,
    kind: str,
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    contribution_keys: dict[str, str] = {}
    ledger: list[dict[str, Any]] = []
    for item in plan["contributions"]:
        page_id = str(item["page_id"])
        contribution = dict(item["contribution"])
        main = kind == "INGEST" or str(contribution["operation_id"]) == draft_id
        contribution_operation = operation_id if main else str(contribution["operation_id"])
        contribution.update({"operation_id": contribution_operation, "page_id": page_id})
        if kind == "LINT" and main:
            contribution["document_id"] = f"lint:{operation_id}"
        contribution_key = object_key(workspace_id, page_id, contribution_operation, "json")
        write_publication_text(
            operation_id,
            page_id,
            contribution_key,
            json.dumps(contribution, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "application/json; charset=utf-8",
            database_connect=connect,
        )
        if main:
            contribution_keys[page_id] = contribution_key
        ledger.append({
            "page_id": page_id,
            "operation_id": contribution_operation,
            "document_id": (
                contribution.get("document_id") if kind == "INGEST"
                else None if contribution.get("artifact_type") == "lint"
                else contribution.get("document_id")
            ),
            "contribution_key": contribution_key,
            "linked_by_operation_id": operation_id if kind == "LINT" and not main else None,
        })
    artifacts: list[dict[str, Any]] = []
    for page in _plan_pages(plan):
        if page["status"] != "active":
            continue
        page_id = str(page["page_id"])
        markdown_key = object_key(workspace_id, page_id, operation_id)
        write_publication_text(operation_id, page_id, markdown_key, str(page["markdown"]), database_connect=connect)
        contribution_key = contribution_keys.get(page_id)
        artifacts.append({
            "page_id": page_id,
            "page_type": page["page_type"],
            "markdown_key": markdown_key,
            "contribution_key": contribution_key,
            "content_hash": "sha256:" + hashlib.sha256(str(page["markdown"]).encode()).hexdigest(),
            "contribution_stored": contribution_key is not None,
        })
    return artifacts, ledger


def _apply_plan(conn, actor, draft, plan, source_blocks, artifacts, ledger, operation_id: str) -> None:
    pages = [page for page in _plan_pages(plan) if page["status"] == "active"]
    artifact_by_page = {item["page_id"]: item for item in artifacts}
    for page in pages:
        conn.execute(
            """
            INSERT INTO wiki_pages(id,page_type,title,slug,summary,markdown_uri,user_id,workspace_id,status,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,'draft',now(),now())
            ON CONFLICT (id) DO NOTHING
            """,
            (
                page["page_id"], page["page_type"], page["title"], page["slug"], page.get("summary"),
                actor.user_id, actor.workspace_id,
            ),
        )
        stored = conn.execute(
            "SELECT page_type,slug,workspace_id FROM wiki_pages WHERE id=%s FOR UPDATE", (page["page_id"],)
        ).fetchone()
        if stored is None or (stored["page_type"], stored["slug"], stored["workspace_id"]) != (
            page["page_type"], page["slug"], actor.workspace_id,
        ):
            raise McpWikiDraftError(409, "PUBLIC_STATE_CHANGED", "Allocated page identity changed.")
    changed_pages = []
    for page in pages:
        artifact = artifact_by_page[str(page["page_id"])]
        changed_pages.append({
            **page,
            "markdown_key": artifact["markdown_key"],
            "source_document_ids": page.get("source_document_ids"),
            "source_document_id": page.get("source_document_id"),
        })
    _apply_restored_wiki_state(
        actor.workspace_id,
        changed_pages,
        {"removed_links": [], "restored_links": []},
        False,
        conn,
        list(plan["deleted_page_ids"]),
    )
    _cleanup_deleted_wiki_pages(conn, actor.workspace_id, list(plan["deleted_page_ids"]))
    affected = [str(page["page_id"]) for page in _plan_pages(plan)]
    conn.execute(
        "DELETE FROM wiki_page_links WHERE workspace_id=%s AND (from_page_id=ANY(%s) OR to_page_id=ANY(%s))",
        (actor.workspace_id, affected, affected),
    )
    for link in plan["wiki_links"]:
        if str(link["from_page_id"]) not in affected and str(link["to_page_id"]) not in affected:
            continue
        conn.execute(
            """
            INSERT INTO wiki_page_links(from_page_id,to_page_id,link_type,label,confidence,workspace_id,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,now(),now())
            ON CONFLICT (from_page_id,link_type,to_page_id) DO UPDATE SET
              label=EXCLUDED.label,confidence=EXCLUDED.confidence,workspace_id=EXCLUDED.workspace_id,updated_at=now()
            """,
            (
                link["from_page_id"], link["to_page_id"], link["link_type"], link.get("label"),
                link.get("confidence"), actor.workspace_id,
            ),
        )
    conn.execute("DELETE FROM wiki_page_entry_references WHERE workspace_id=%s AND page_id=ANY(%s)", (actor.workspace_id, affected))
    for ref in plan["entry_references"]:
        if str(ref["page_id"]) not in affected:
            continue
        conn.execute(
            """
            INSERT INTO wiki_page_entry_references(page_id,entry_id,workspace_id,target_slug,name,parent_entry_id)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (ref["page_id"], ref["entry_id"], actor.workspace_id, ref["target_slug"], ref["name"], ref.get("parent_entry_id")),
        )
    if draft["draft_kind"] == "INGEST":
        document_id = str(draft["document_id"])
        conn.execute("DELETE FROM source_blocks WHERE document_id=%s", (document_id,))
        for block in source_blocks:
            conn.execute(
                "INSERT INTO source_blocks(document_id,block_id,text,position) VALUES (%s,%s,%s,%s)",
                (document_id, block["block_id"], block["text"], block["ordinal"]),
            )
        conn.execute(
            """
            INSERT INTO source_block_snapshots(document_id,source_content_hash) VALUES (%s,%s)
            ON CONFLICT (document_id) DO UPDATE SET source_content_hash=EXCLUDED.source_content_hash,updated_at=now()
            """,
            (document_id, str(draft["source_content_hash"]).removeprefix("sha256:")),
        )
    pending = [
        {
            "page_id": item["page_id"],
            "operation_id": item["operation_id"],
            "document_id": item["document_id"],
            "object_key": item["contribution_key"],
            "active": True,
            "linked_by_operation_id": item["linked_by_operation_id"],
        }
        for item in ledger
    ]
    record_pending_deltas(conn, actor.workspace_id, pending, publication_id=publication_id(operation_id))


def _plan_has_effect(conn, workspace_id: str, draft, plan, source_blocks) -> bool:
    pages = _plan_pages(plan)
    ids = [str(page["page_id"]) for page in pages]
    current = conn.execute(
        "SELECT id,page_type,title,slug,summary,status,markdown_uri FROM wiki_pages WHERE workspace_id=%s AND id=ANY(%s)",
        (workspace_id, ids),
    ).fetchall()
    if len(current) != len(pages):
        return True
    from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import read_optional_text_object

    by_id = {str(row["id"]): row for row in current}
    for page in pages:
        row = by_id[str(page["page_id"])]
        if (
            row["page_type"] != page["page_type"]
            or row["title"] != page["title"]
            or row["slug"] != page["slug"]
            or (row["summary"] or "") != (page.get("summary") or "")
            or row["status"] != page["status"]
            or read_optional_text_object(str(row["markdown_uri"] or "")) != str(page["markdown"])
        ):
            return True
    # New ledger deltas are publication effects even when a rebuilt body happens to be byte-identical.
    if plan["contributions"]:
        return True
    desired_entries = [
        {"page_id": str(page["page_id"]), **entry}
        for page in pages
        for entry in page.get("entries") or []
    ]
    current_entries = [dict(row) for row in conn.execute(
        "SELECT page_id,entry_id,name,parent_entry_id,definition,block_refs,slugs,aliases,tags "
        "FROM wiki_page_entries WHERE workspace_id=%s AND page_id=ANY(%s)",
        (workspace_id, ids),
    ).fetchall()]
    desired_aliases = [
        {"page_id": str(page["page_id"]), "alias": alias, "origin": "extracted"}
        for page in pages
        for alias in page.get("identity_aliases") or []
    ]
    current_aliases = [dict(row) for row in conn.execute(
        "SELECT page_id,alias,origin FROM wiki_concept_identity_aliases WHERE workspace_id=%s AND page_id=ANY(%s)",
        (workspace_id, ids),
    ).fetchall()]
    current_document_links = [dict(row) for row in conn.execute(
        "SELECT document_id,wiki_page_id,relation_type,confidence FROM document_wiki_links "
        "WHERE workspace_id=%s AND wiki_page_id=ANY(%s)",
        (workspace_id, ids),
    ).fetchall()]
    current_blocks = []
    desired_blocks = []
    if draft["draft_kind"] == "INGEST":
        current_blocks = [dict(row) for row in conn.execute(
            "SELECT block_id,text FROM source_blocks WHERE document_id=%s ORDER BY position NULLS LAST,block_id",
            (draft["document_id"],),
        ).fetchall()]
        desired_blocks = [{"block_id": item["block_id"], "text": item["text"]} for item in source_blocks]
    if any((
        _set_hash(current_entries) != _set_hash(desired_entries),
        _set_hash(current_aliases) != _set_hash(desired_aliases),
        _set_hash(current_document_links) != _set_hash([
            item for item in plan["document_links"] if str(item["wiki_page_id"]) in ids
        ]),
        _set_hash(current_blocks) != _set_hash(desired_blocks),
    )):
        return True
    current_links = [dict(row) for row in conn.execute(
        "SELECT from_page_id,to_page_id,link_type,label,confidence FROM wiki_page_links "
        "WHERE workspace_id=%s AND (from_page_id=ANY(%s) OR to_page_id=ANY(%s))",
        (workspace_id, ids, ids),
    ).fetchall()]
    current_refs = [dict(row) for row in conn.execute(
        "SELECT page_id,target_slug,entry_id,parent_entry_id,name FROM wiki_page_entry_references "
        "WHERE workspace_id=%s AND page_id=ANY(%s)",
        (workspace_id, ids),
    ).fetchall()]
    desired_links = [
        item for item in plan["wiki_links"]
        if str(item["from_page_id"]) in ids or str(item["to_page_id"]) in ids
    ]
    desired_refs = [item for item in plan["entry_references"] if str(item["page_id"]) in ids]
    return _set_hash(current_links) != _set_hash(desired_links) or _set_hash(current_refs) != _set_hash(desired_refs)


def _set_hash(rows: list[dict[str, Any]]) -> list[bytes]:
    return sorted(_canonical(row) for row in rows)


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
