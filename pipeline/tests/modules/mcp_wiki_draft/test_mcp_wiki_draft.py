from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

import api
from app.modules.task_cancellation.infrastructure import postgres_task_journal
from app.modules.data_purge.infrastructure import postgres_purge
from app.modules.mcp_wiki_draft.application import (
    McpWikiDraftError,
    McpWikiDraftIntake,
    canonical_hash,
    request_actor,
)
from app.modules.mcp_wiki_draft.infrastructure.postgres_repository import (
    PostgresMcpWikiDraftRepository,
    expire_inactive_runs,
)
from app.modules.mcp_wiki_draft.infrastructure.backend_source_reader import BackendMcpWikiSourceReader
from app.modules.mcp_wiki_draft.infrastructure import backend_source_reader
from app.modules.mcp_wiki_draft.infrastructure import apply_repository as apply_repository_module
from app.modules.mcp_wiki_draft.apply_application import McpWikiDraftApply
from app.modules.mcp_wiki_draft.infrastructure.apply_repository import PostgresMcpWikiApplyRepository
from app.modules.mcp_wiki_draft.interfaces.http.apply_schemas import ApplyWikiDraftRequest
from app.modules.mcp_wiki_draft import calculation
from app.modules.mcp_wiki_draft.interfaces.http.dependencies import get_mcp_wiki_draft_intake
from app.modules.mcp_wiki_draft.interfaces.http.schemas import (
    FinalizeIngestRequest,
    McpWikiActorRequest,
    StartIngestRequest,
    SubmitLintDraftRequest,
    SubmitWindowRequest,
)
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import rebuild_concept_page
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as wiki_repository
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_output_persistence as output_persistence
from app.modules.wiki_ingestion.infrastructure.document_ingest_lock import ActiveIngestRunError
from app.modules.wiki_ingestion.infrastructure.object_storage import client as storage_client, read_text_object, write_text_object


CONFIG = {
    "MCP_WIKI_DRAFT_MAX_REQUEST_BYTES": "100000",
    "MCP_WIKI_DRAFT_SOURCE_TIMEOUT_SECONDS": "7",
    "MCP_WIKI_DRAFT_SOURCE_RESPONSE_MAX_BYTES": "100000",
    "MCP_WIKI_DRAFT_SOURCE_MAX_BLOCKS": "20",
    "MCP_WIKI_DRAFT_SOURCE_MAX_TEXT_BYTES": "10000",
    "MCP_WIKI_DRAFT_WINDOW_MAX_BLOCKS": "1",
    "MCP_WIKI_DRAFT_WINDOW_MAX_TEXT_BYTES": "1000",
    "MCP_WIKI_DRAFT_MAX_ACTIVE_PER_USER": "4",
    "MCP_WIKI_DRAFT_MAX_ACTIVE_PER_WORKSPACE": "8",
    "MCP_WIKI_DRAFT_MAX_ACTIVE_STORAGE_BYTES": "100000",
    "MCP_WIKI_DRAFT_RUN_TTL_SECONDS": "3600",
    "MCP_WIKI_DRAFT_MAX_CONTRIBUTIONS": "5",
    "MCP_WIKI_DRAFT_MAX_STATEMENTS": "10",
    "MCP_WIKI_DRAFT_MAX_EVIDENCE_PER_STATEMENT": "3",
    "MCP_WIKI_DRAFT_MAX_LINKS": "5",
    "MCP_WIKI_DRAFT_MAX_NAME_CHARS": "100",
    "MCP_WIKI_DRAFT_MAX_STATEMENT_CHARS": "500",
    "MCP_WIKI_DRAFT_MAX_QUOTE_CHARS": "500",
    "MCP_WIKI_DRAFT_LEXICAL_OVERLAP_THRESHOLD": "0.5",
    "MCP_WIKI_DRAFT_INSTRUCTION_PATTERNS_JSON": '["(?i)ignore previous"]',
    "MCP_WIKI_DRAFT_VALIDATION_POLICY_VERSION": "test-v1",
    "MCP_WIKI_DRAFT_VALIDATION_EVALSET_ID": "test-set",
    "MCP_WIKI_DRAFT_PREVIEW_MAX_ITEMS": "1000",
    "MCP_WIKI_DRAFT_PREVIEW_MAX_BYTES": "1000000",
    "MCP_WIKI_DRAFT_PREVIEW_MAX_SOURCE_BYTES": "100000",
    "MCP_WIKI_DRAFT_LINT_MAX_OPERATIONS": "20",
    "MCP_WIKI_DRAFT_LINT_MAX_TARGET_PAGES": "20",
    "MCP_WIKI_DRAFT_LINT_MAX_SOURCE_SNAPSHOTS": "5",
    "MCP_WIKI_DRAFT_LINT_MAX_TOTAL_SOURCE_BYTES": "100000",
    "MCP_WIKI_DRAFT_LINT_MAX_STRUCTURAL_CHANGES": "100",
    "MCP_WIKI_DRAFT_LINT_MAX_DELETES": "20",
    "MCP_WIKI_DRAFT_LINT_TTL_SECONDS": "3600",
    "MCP_WIKI_DRAFT_LINT_RELATIONS_JSON": '["concept_related_to"]',
    "MCP_WIKI_DRAFT_LINT_POLICY_VERSION": "lint-test-v1",
}


def _connect():
    return psycopg.connect(os.environ["AI_DATABASE_URL"], row_factory=dict_row)


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    if not os.environ.get("AI_DATABASE_URL"):
        pytest.skip("AI_DATABASE_URL is required")
    for key, value in CONFIG.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AGENT_INTERNAL_TOKEN", "agent-test-token")
    # MCP 편입은 웹 편입 반영 코드를 그대로 탄다. 테스트가 바꾼 원장 조회를 웹 반영에도 쓴다.
    for module in (output_persistence, wiki_repository):
        monkeypatch.setattr(module, "read_contributions", lambda *args: calculation.read_contributions(*args))
    with _connect() as conn:
        conn.execute(
            "TRUNCATE mcp_wiki_publication_outbox,mcp_wiki_publication_results,mcp_wiki_draft_idempotency,mcp_wiki_lint_source_blocks,mcp_wiki_drafts,mcp_wiki_window_submissions,mcp_wiki_windows,"
            "mcp_wiki_source_blocks,mcp_wiki_ingest_runs,pipeline_runs,ai_task_changes,ai_task_runs,ai_purged_scopes,"
            "wiki_page_entry_references,wiki_page_entries,wiki_source_manual_links,document_wiki_links,wiki_page_links,"
            "wiki_pending_contributions,wiki_publications,wiki_pages,source_blocks,source_block_snapshots,document_derived_state,"
            "wiki_page_embeddings,wiki_embedding_units,wiki_embedding_vectors,document_quality_reviews CASCADE"
        )
    api.app.dependency_overrides.pop(get_mcp_wiki_draft_intake, None)
    yield
    api.app.dependency_overrides.pop(get_mcp_wiki_draft_intake, None)


def _actor(tool: str, grant_id: str, **changes) -> dict:
    value = {
        "invocation_id": str(uuid4()), "grant_id": grant_id, "user_id": "user-1",
        "workspace_id": "workspace-1", "client_id": "https://client.example/metadata",
        "policy_generation": 1, "origin": "MCP_CLIENT", "spec_version": "2025-11-25",
        "tool_name": tool,
    }
    value.update(changes)
    return value


class Source:
    def __init__(self, blocks: list[tuple[str, str]] | None = None, display_name: str = "Source document"):
        self.blocks = blocks or [("B0001", "Alpha concept has evidence."), ("B0002", "Beta concept has support.")]
        self.display_name = display_name
        self.calls = 0

    def read(self, actor, document_id, _limits):
        self.calls += 1
        records = [
            {"block_id": block_id, "ordinal": ordinal, "text": text,
             "content_hash": "sha256:" + sha256(text.encode()).hexdigest()}
            for ordinal, (block_id, text) in enumerate(self.blocks, 1)
        ]
        body = "".join(record["text"] for record in records)
        return {
            "schema_version": "mcp.wiki-source.v1", "document_id": document_id,
            "display_name": self.display_name,
            "workspace_id": actor.workspace_id, "source_version": 7,
            "source_content_hash": "sha256:" + sha256(body.encode()).hexdigest(),
            "mime_type": "text/markdown", "blocks": records,
        }


def _start_payload(grant_id: str, document_id: str = "doc_1", key: str = "start-1", **actor_changes):
    actor = McpWikiActorRequest.model_validate(_actor("start_ingest", grant_id, **actor_changes))
    digest = canonical_hash({
        "schema_version": "mcp.wiki-draft.v1", "actor": request_actor(actor), "document_id": document_id,
    })
    return StartIngestRequest.model_validate({
        "schema_version": "mcp.wiki-draft.v1", "actor": actor.model_dump(),
        "idempotency_key": key, "request_hash": digest, "document_id": document_id,
    })


def _submission(grant_id: str, start: dict, window: dict, *, key="submit-1", concept="Alpha", text="Alpha concept has evidence.", quote="Alpha concept has evidence.", block_id="B0001", merge_target=None):
    actor = McpWikiActorRequest.model_validate(_actor("submit_window", grant_id))
    contributions = [{
        "concept_name": concept, "statements": [{
            "statement_id": "statement-1", "text": text,
            "evidence": [{"snapshot_id": start["source_snapshot"]["snapshot_id"], "block_id": block_id, "quote": quote}],
        }], "links": [],
    }]
    if merge_target is not None:
        contributions[0]["merge_target_page_id"] = merge_target
    mapping = [{
        "snapshot_id": start["source_snapshot"]["snapshot_id"], "document_id": "doc_1",
        "source_version": 7, "source_content_hash": start["source_snapshot"]["source_content_hash"],
    }]
    digest = canonical_hash({
        "schema_version": "mcp.wiki-draft.v1", "actor": request_actor(actor),
        "run_id": start["run_id"], "window_id": window["window_id"],
        "snapshot_id": start["source_snapshot"]["snapshot_id"], "window_hash": window["window_hash"],
        "source_snapshots": mapping, "contributions": contributions,
    })
    return SubmitWindowRequest.model_validate({
        "schema_version": "mcp.wiki-draft.v1", "actor": actor.model_dump(), "idempotency_key": key,
        "request_hash": digest, "snapshot_id": start["source_snapshot"]["snapshot_id"],
        "window_hash": window["window_hash"], "contributions": contributions,
    })


def _finalize_payload(grant_id: str, start: dict, *, key="finalize-1", count: int | None = None):
    actor = McpWikiActorRequest.model_validate(_actor("finalize_ingest", grant_id))
    with _connect() as conn:
        run = conn.execute("SELECT * FROM mcp_wiki_ingest_runs WHERE run_id=%s", (start["run_id"],)).fetchone()
        rows = conn.execute(
            "SELECT win.window_id,win.window_ordinal,win.window_hash,win.accepted_submission_hash,submission.contributions "
            "FROM mcp_wiki_windows win LEFT JOIN mcp_wiki_window_submissions submission USING(window_id) "
            "WHERE win.run_id=%s ORDER BY win.window_ordinal",
            (start["run_id"],),
        ).fetchall()
    submissions = [{
        "window_id": str(row["window_id"]), "window_ordinal": row["window_ordinal"],
        "window_hash": row["window_hash"], "accepted_submission_hash": row["accepted_submission_hash"],
        "contributions": list(row["contributions"] or []),
    } for row in rows]
    expected = count if count is not None else int(run["submitted_window_count"])
    digest = canonical_hash({
        "schema_version": "mcp.wiki-draft.v1", "actor": request_actor(actor),
        "run_id": start["run_id"], "snapshot_id": start["source_snapshot"]["snapshot_id"],
        "expected_submitted_window_count": expected,
        "source_snapshots": [{
            "snapshot_id": str(run["snapshot_id"]), "document_id": run["document_id"],
            "source_version": run["source_version"], "source_content_hash": run["source_content_hash"],
        }],
        "submissions": submissions,
    })
    return FinalizeIngestRequest.model_validate({
        "schema_version": "mcp.wiki-draft.v1", "actor": actor.model_dump(),
        "idempotency_key": key, "request_hash": digest,
        "snapshot_id": start["source_snapshot"]["snapshot_id"],
        "expected_submitted_window_count": expected,
    })


def _complete_intake(intake, grant: str, *, start_key: str = "start-1"):
    started = intake.start(_start_payload(grant, key=start_key))
    first = intake.read_window(started["run_id"], McpWikiActorRequest.model_validate(_actor("get_window", grant)), 1)
    intake.submit(started["run_id"], first["window_id"], _submission(grant, started, first))
    second = intake.read_window(started["run_id"], McpWikiActorRequest.model_validate(_actor("get_window", grant)), 2)
    intake.submit(started["run_id"], second["window_id"], _submission(
        grant, started, second, key="submit-2", concept="Beta", text="Beta concept has support.",
        quote="Beta concept has support.", block_id="B0002",
    ))
    return started


def _intake(source=None):
    return McpWikiDraftIntake(PostgresMcpWikiDraftRepository(), source or Source())


class _ValidApprovalAuthority:
    def lookup(self, request):
        assert request["schema_version"] == "mcp.wiki-approval-lookup.v1"
        assert request["source_snapshots"]
        return {"schema_version": "mcp.wiki-approval-status.v1", "status": "VALID", "reason": None}


class _UnknownApprovalAuthority:
    def lookup(self, request):
        return {
            "schema_version": "mcp.wiki-approval-status.v1",
            "status": "UNKNOWN",
            "reason": "AUTHZ_UNAVAILABLE",
        }


def _apply_payload(grant_id: str, preview: dict, **changes) -> ApplyWikiDraftRequest:
    value = {
        "schema_version": "mcp.wiki-apply.v1",
        "actor": _actor("apply_wiki_draft", grant_id),
        "approval_id": str(uuid4()),
        # 불변 발행 객체 key에 들어가므로 테스트마다 다른 작업 id를 쓴다(같은 key·다른 본문은 충돌이다).
        "operation_id": f"document-apply-operation-{uuid4()}",
        "draft_id": preview["draft_id"],
        "draft_version": preview["draft_version"],
        "draft_hash": preview["draft_hash"],
        "public_state_hash": preview["public_state_hash"],
        "approval_mode": "EXPLICIT_WEB",
    }
    value.update(changes)
    return ApplyWikiDraftRequest.model_validate(value)


def _stored_contribution(page_id: str, slug: str, operation_id: str, document_id: str, ref: str, *, links=(), entries=()):
    evidence_id = f"{operation_id}:evidence"
    value = {
        "schema_version": 1,
        "operation_id": operation_id,
        "document_id": document_id,
        "page_id": page_id,
        "concept": {
            "slug": slug, "title": slug.title(), "definition": f"{slug} definition",
            "identity_aliases": [], "anchor_reference_ids": [ref],
            "evidence_claim_ids": [evidence_id], "source_document_ids": [document_id],
        },
        "evidence_units": [{
            "evidence_id": evidence_id, "claim": f"{slug} evidence",
            "anchor_reference_ids": [ref], "source_document_id": document_id,
        }],
        "source_key_points": [{"text": f"{slug} evidence", "anchor_reference_ids": [ref]}],
        "links": list(links),
        "source_blocks": [],
    }
    if entries:
        value["entry_changes"] = list(entries)
    write_text_object(f"wiki/workspace-1/pages/{page_id}/ops/{operation_id}.json", json.dumps(value))
    return {
        "page_id": page_id, "ingest_operation_id": operation_id,
        "source_document_id": document_id, "sequence_revision": 1,
        "active": True, "object_key": f"wiki/workspace-1/pages/{page_id}/ops/{operation_id}.json",
        "lint_operation_id": None,
    }


def _seed_lint_page(page_id: str, slug: str, *, links=(), entries=()):
    operation_id = f"seed-{slug}"
    ledger = _stored_contribution(
        page_id, slug, operation_id, "doc_seed", "doc_seed:B1", links=links, entries=entries
    )
    contribution = json.loads(read_text_object(ledger["object_key"]))
    rebuilt = rebuild_concept_page([
        PageContribution(page_id, "concept", operation_id, 1, "", contribution)
    ])
    uri = write_text_object(f"baseline/{page_id}.md", rebuilt.markdown)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,summary,markdown_uri,user_id,workspace_id,status,created_at,updated_at) "
            "VALUES (%s,'concept',%s,%s,%s,%s,'user-1','workspace-1','active',now(),now())",
            (page_id, slug.title(), slug, f"{slug} definition", uri),
        )
        snapshot = calculation._page_snapshot(dict(conn.execute(
            "SELECT * FROM wiki_pages WHERE id=%s", (page_id,),
        ).fetchone()))
    return ledger, {key: snapshot[key] for key in ("page_id", "version", "content_hash")}, f"{operation_id}:evidence"


def _lint_payload(grant: str, targets: list[dict], operations, source: Source, *, key="lint-1"):
    actor = McpWikiActorRequest.model_validate(_actor("submit_lint_changes", grant))
    document_id = "doc_lint"
    authoritative = source.read(actor, document_id, None)
    mappings = [{
        "snapshot_id": str(uuid4()), "document_id": document_id,
        "source_version": authoritative["source_version"],
        "source_content_hash": authoritative["source_content_hash"],
    }]
    if callable(operations):
        operations = operations(mappings[0]["snapshot_id"])
    value = {
        "schema_version": "mcp.wiki-draft.v1", "actor": actor.model_dump(),
        "idempotency_key": key, "request_hash": "sha256:" + "0" * 64,
        "target_snapshot": targets, "source_snapshots": mappings, "operations": operations,
    }
    payload = SubmitLintDraftRequest.model_validate(value)
    command = {
        "schema_version": payload.schema_version, "actor": request_actor(payload.actor),
        "target_snapshot": sorted((item.model_dump() for item in payload.target_snapshot), key=lambda item: item["page_id"]),
        "source_snapshots": sorted((item.model_dump() for item in payload.source_snapshots), key=lambda item: item["snapshot_id"]),
        "operations": [item.model_dump() for item in payload.operations],
    }
    return payload.model_copy(update={"request_hash": canonical_hash(command)}), mappings[0]["snapshot_id"]


def _lint_apply_draft(monkeypatch):
    ledger, target, statement_id = _seed_lint_page("alpha-page", "alpha")
    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: [ledger])
    source = Source([("B0001", "Alpha replacement evidence.")])
    grant = str(uuid4())
    lint_payload, _snapshot = _lint_payload(
        grant,
        [target],
        lambda snapshot: [{
            "operation_id": "replace-alpha", "kind": "UPSERT_STATEMENT",
            "target_page_id": "alpha-page", "depends_on": [],
            "arguments": {
                "statement_id": statement_id, "text": "Alpha replacement evidence.",
                "evidence": [{
                    "snapshot_id": snapshot, "block_id": "B0001",
                    "quote": "Alpha replacement evidence.",
                }],
            },
        }],
        source,
    )
    return grant, _intake(source).lint(lint_payload)


def test_lint_upsert_is_immutable_owner_scoped_and_side_effect_free(monkeypatch):
    ledger, target, statement_id = _seed_lint_page("alpha-page", "alpha")
    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: [ledger])
    source = Source([("B0001", "Alpha replacement evidence.")])
    grant = str(uuid4())
    payload, snapshot_id = _lint_payload(
        grant,
        [target],
        lambda snapshot: [{
            "operation_id": "replace-alpha", "kind": "UPSERT_STATEMENT",
            "target_page_id": "alpha-page", "depends_on": [],
            "arguments": {
                "statement_id": statement_id, "text": "Alpha replacement evidence.",
                "evidence": [{
                    "snapshot_id": snapshot, "block_id": "B0001",
                    "quote": "Alpha replacement evidence.",
                }],
            },
        }],
        source,
    )
    source.calls = 0
    with _connect() as conn:
        public_before = {
            table: conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"]
            for table in (
                "wiki_pages", "wiki_page_entries", "wiki_page_entry_references", "wiki_page_links",
                "wiki_publications", "wiki_publication_outbox", "wiki_pending_contributions", "ai_task_changes",
            )
        }
    storage = storage_client()
    bucket = os.environ["S3_BUCKET"]
    objects_before = [(item.object_name, item.etag, item.size) for item in storage.list_objects(bucket, recursive=True)]

    intake = _intake(source)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: intake.lint(payload), range(2)))
    assert results[0] == results[1]
    result = results[0]
    api.app.dependency_overrides[get_mcp_wiki_draft_intake] = lambda: intake
    with TestClient(api.app) as client:
        routed = client.post(
            "/internal/mcp/wiki/lint-drafts",
            headers={"X-Agent-Service-Token": "agent-test-token"},
            content=payload.model_dump_json(),
        )
    assert routed.status_code == 200 and routed.json() == result
    assert set(result) == {
        "schema_version", "draft_id", "draft_version", "draft_hash", "public_state_hash", "state"
    }
    assert result["state"] == "AWAITING_WEB_APPROVAL" and result["draft_version"] == 1
    preview = intake.preview(result["draft_id"], 1, "workspace-1", "user-1")
    assert preview["complete"] is True and preview["draft_kind"] == "LINT"
    assert preview["source_snapshots"][0]["snapshot_id"] == snapshot_id
    assert any(
        item["kind"] == "CONCEPT_STATEMENT" and item["action"] == "UPDATE"
        and item["subject_id"] == statement_id and item["evidence"]
        for item in preview["changes"]
    )
    with pytest.raises(McpWikiDraftError) as hidden:
        intake.preview(result["draft_id"], 1, "workspace-1", "user-other")
    assert hidden.value.code == "NOT_FOUND"
    assert objects_before == [(item.object_name, item.etag, item.size) for item in storage.list_objects(bucket, recursive=True)]
    with _connect() as conn:
        assert public_before == {
            table: conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"]
            for table in public_before
        }
        draft = conn.execute("SELECT * FROM mcp_wiki_drafts WHERE draft_id=%s", (result["draft_id"],)).fetchone()
        assert draft["draft_kind"] == "LINT" and draft["run_id"] is None
        assert conn.execute(
            "SELECT count(*) AS count FROM mcp_wiki_lint_source_blocks WHERE draft_id=%s",
            (result["draft_id"],),
        ).fetchone()["count"] == 1
        assert conn.execute(
            "SELECT kind,status FROM ai_task_runs WHERE id=%s", (result["draft_id"],),
        ).fetchone()["kind"] == "mcp_wiki_lint"

    retry = payload.model_copy(update={
        "actor": payload.actor.model_copy(update={"invocation_id": str(uuid4())}),
        "idempotency_key": "lint-domain-replay",
    })
    assert intake.lint(retry) == result
    different, _snapshot = _lint_payload(
        grant, [target], [{
            "operation_id": "remove-alpha", "kind": "REMOVE_STATEMENT",
            "target_page_id": "alpha-page", "depends_on": [],
            "arguments": {"statement_id": statement_id, "expected_text_hash": "sha256:" + "0" * 64},
        }], source, key="lint-1",
    )
    with pytest.raises(McpWikiDraftError) as conflict:
        intake.lint(different)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_explicit_lint_apply_recalculates_and_atomically_publishes(monkeypatch):
    grant, finalized = _lint_apply_draft(monkeypatch)
    payload = _apply_payload(grant, finalized, operation_id="document-lint-operation-1")
    apply = McpWikiDraftApply(PostgresMcpWikiApplyRepository(), _ValidApprovalAuthority())
    first = apply.apply(payload.draft_id, payload)
    retry = payload.model_copy(update={
        "actor": payload.actor.model_copy(update={"invocation_id": str(uuid4())})
    })
    assert apply.apply(retry.draft_id, retry) == first
    assert first["state"] == "PUBLISHED" and first["changed"] is True

    with _connect() as conn:
        page = conn.execute("SELECT markdown_uri FROM wiki_pages WHERE id='alpha-page'").fetchone()
        event = conn.execute("SELECT payload FROM mcp_wiki_publication_outbox").fetchone()["payload"]
        pending = conn.execute(
            "SELECT operation_id,document_id,linked_by_operation_id FROM wiki_pending_contributions"
        ).fetchone()
        assert conn.execute(
            "SELECT state FROM mcp_wiki_drafts WHERE draft_id=%s", (payload.draft_id,)
        ).fetchone()["state"] == "PUBLISHED"
        assert conn.execute(
            "SELECT status FROM ai_task_runs WHERE id=%s", (payload.draft_id,)
        ).fetchone()["status"] == "completed"
        assert conn.execute("SELECT count(*) AS count FROM wiki_publication_outbox").fetchone()["count"] == 1
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_publication_outbox").fetchone()["count"] == 1
    assert "Alpha replacement evidence." in read_text_object(page["markdown_uri"])
    assert event["kind"] == "LINT" and event["outcome"] == "PUBLISHED"
    assert pending == {
        "operation_id": payload.operation_id,
        "document_id": None,
        "linked_by_operation_id": None,
    }

    delivered = []
    monkeypatch.setattr(apply_repository_module, "_send_publication_receipt", delivered.append)
    assert asyncio.run(apply_repository_module.relay_pending_mcp_publications()) == 1
    assert delivered == [event]
    with _connect() as conn:
        assert conn.execute("SELECT state FROM mcp_wiki_publication_outbox").fetchone()["state"] == "sent"


def test_lint_apply_target_drift_conflicts_without_publication(monkeypatch):
    grant, finalized = _lint_apply_draft(monkeypatch)
    with _connect() as conn:
        conn.execute("UPDATE wiki_pages SET updated_at=now()+interval '1 second' WHERE id='alpha-page'")
    payload = _apply_payload(grant, finalized, operation_id="document-lint-operation-drift")
    result = McpWikiDraftApply(
        PostgresMcpWikiApplyRepository(), _ValidApprovalAuthority()
    ).apply(payload.draft_id, payload)
    assert result["state"] == "CONFLICTED" and result["error_code"] == "PUBLIC_STATE_CHANGED"
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM wiki_publications").fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_publication_outbox").fetchone()["count"] == 0


def test_lint_apply_does_not_duplicate_unaffected_target_references(monkeypatch):
    alpha, alpha_target, statement_id = _seed_lint_page("alpha-page", "alpha")
    beta, beta_target, _beta_statement = _seed_lint_page("beta-page", "beta")
    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: [alpha, beta])
    with _connect() as conn:
        conn.execute(
            "INSERT INTO wiki_page_entry_references(page_id,entry_id,workspace_id,target_slug,name,parent_entry_id) "
            "VALUES ('beta-page','ref-entry','workspace-1','beta','Beta',NULL)"
        )
    source = Source([("B0001", "Alpha replacement evidence.")])
    grant = str(uuid4())
    lint_payload, _snapshot = _lint_payload(
        grant,
        [alpha_target, beta_target],
        lambda snapshot: [{
            "operation_id": "replace-alpha", "kind": "UPSERT_STATEMENT",
            "target_page_id": "alpha-page", "depends_on": [],
            "arguments": {
                "statement_id": statement_id, "text": "Alpha replacement evidence.",
                "evidence": [{
                    "snapshot_id": snapshot, "block_id": "B0001",
                    "quote": "Alpha replacement evidence.",
                }],
            },
        }],
        source,
    )
    finalized = _intake(source).lint(lint_payload)
    payload = _apply_payload(grant, finalized, operation_id="document-lint-partial-target")
    assert McpWikiDraftApply(
        PostgresMcpWikiApplyRepository(), _ValidApprovalAuthority()
    ).apply(payload.draft_id, payload)["state"] == "PUBLISHED"
    with _connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS count FROM wiki_page_entry_references "
            "WHERE page_id='beta-page' AND entry_id='ref-entry'"
        ).fetchone()["count"] == 1


def test_lint_apply_unknown_then_cancel_halts_and_removes_private_draft(monkeypatch):
    grant, finalized = _lint_apply_draft(monkeypatch)
    payload = _apply_payload(grant, finalized, operation_id="document-lint-operation-cancel")
    apply = McpWikiDraftApply(PostgresMcpWikiApplyRepository(), _UnknownApprovalAuthority())
    assert apply.apply(payload.draft_id, payload)["state"] == "UNKNOWN"
    postgres_task_journal.request_cancel(payload.draft_id, "workspace-1", "user-1")
    postgres_task_journal.rollback(payload.draft_id)
    result = apply.result(payload.approval_id, payload.operation_id, "workspace-1", "user-1")
    assert result["state"] == "HALTED" and result["error_code"] == "CANCELLED"
    with _connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS count FROM mcp_wiki_drafts WHERE draft_id=%s", (payload.draft_id,)
        ).fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM wiki_publications").fetchone()["count"] == 0


def test_mcp_publication_receipt_failure_stays_pending_then_retries(monkeypatch):
    grant, finalized = _lint_apply_draft(monkeypatch)
    payload = _apply_payload(grant, finalized, operation_id="document-lint-receipt-retry")
    McpWikiDraftApply(
        PostgresMcpWikiApplyRepository(), _ValidApprovalAuthority()
    ).apply(payload.draft_id, payload)

    def unavailable(_event):
        raise RuntimeError("controlled receiver conflict")

    monkeypatch.setattr(apply_repository_module, "_send_publication_receipt", unavailable)
    with pytest.raises(RuntimeError, match="controlled receiver conflict"):
        asyncio.run(apply_repository_module.relay_pending_mcp_publications())
    with _connect() as conn:
        row = conn.execute(
            "SELECT state,attempts FROM mcp_wiki_publication_outbox"
        ).fetchone()
    assert row == {"state": "pending", "attempts": 1}

    delivered = []
    monkeypatch.setattr(apply_repository_module, "_send_publication_receipt", delivered.append)
    with _connect() as conn:
        conn.execute("UPDATE mcp_wiki_publication_outbox SET next_attempt_at=now()")
    assert asyncio.run(apply_repository_module.relay_pending_mcp_publications()) == 1
    assert len(delivered) == 1
    with _connect() as conn:
        assert conn.execute(
            "SELECT state,attempts FROM mcp_wiki_publication_outbox"
        ).fetchone() == {"state": "sent", "attempts": 2}


def test_lint_remove_then_delete_orphan_and_cancel_is_terminal(monkeypatch):
    ledger, target, statement_id = _seed_lint_page("alpha-page", "alpha")
    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: [ledger])
    source = Source([("B0001", "Controlled lint context.")])
    grant = str(uuid4())
    payload, _snapshot = _lint_payload(
        grant,
        [target],
        [
            {
                "operation_id": "remove-alpha", "kind": "REMOVE_STATEMENT",
                "target_page_id": "alpha-page", "depends_on": [],
                "arguments": {
                    "statement_id": statement_id,
                    "expected_text_hash": "sha256:" + sha256(b"alpha evidence").hexdigest(),
                },
            },
            {
                "operation_id": "delete-alpha", "kind": "DELETE_ORPHAN_PAGE",
                "target_page_id": "alpha-page", "depends_on": ["remove-alpha"],
                "arguments": {"page_id": "alpha-page", "expected_content_hash": target["content_hash"]},
            },
        ],
        source,
    )
    intake = _intake(source)
    result = intake.lint(payload)
    preview = intake.preview(result["draft_id"], 1, "workspace-1", "user-1")
    assert any(item["kind"] == "CONCEPT_STATEMENT" and item["action"] == "REMOVE" for item in preview["changes"])
    assert any(item["kind"] == "PAGE_DELETE" and item["page_id"] == "alpha-page" for item in preview["changes"])

    postgres_task_journal.request_cancel(result["draft_id"], "workspace-1", "user-1")
    postgres_task_journal.rollback(result["draft_id"])
    with pytest.raises(McpWikiDraftError) as gone:
        intake.preview(result["draft_id"], 1, "workspace-1", "user-1")
    assert gone.value.code == "NOT_FOUND"
    with pytest.raises(McpWikiDraftError) as late:
        intake.lint(payload)
    assert late.value.code == "NOT_FOUND"


def test_lint_add_then_remove_link_replays_structure_without_invented_evidence(monkeypatch):
    alpha, alpha_target, _alpha_statement = _seed_lint_page("alpha-page", "alpha")
    beta, beta_target, _beta_statement = _seed_lint_page("beta-page", "beta")
    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: [alpha, beta])
    source = Source([("B0001", "Controlled lint context.")])
    grant = str(uuid4())
    payload, _snapshot = _lint_payload(
        grant,
        [alpha_target, beta_target],
        [
            {
                "operation_id": "add-related", "kind": "ADD_LINK", "target_page_id": "alpha-page",
                "depends_on": [], "arguments": {
                    "source_ref": "concept:alpha", "target_ref": "concept:beta",
                    "relation": "concept_related_to",
                },
            },
            {
                "operation_id": "remove-related", "kind": "REMOVE_LINK", "target_page_id": "alpha-page",
                "depends_on": ["add-related"], "arguments": {
                    "source_ref": "concept:alpha", "target_ref": "concept:beta",
                    "relation": "concept_related_to",
                },
            },
        ],
        source,
    )
    result = _intake(source).lint(payload)
    with _connect() as conn:
        stored = conn.execute(
            "SELECT normalized_submissions,changes FROM mcp_wiki_drafts WHERE draft_id=%s",
            (result["draft_id"],),
        ).fetchone()
    assert [item["kind"] for item in stored["normalized_submissions"]] == ["ADD_LINK", "REMOVE_LINK"]
    assert all(not item["evidence"] for item in stored["changes"] if item["kind"] != "CONCEPT_STATEMENT")


def test_lint_merge_uses_closed_split_identity_rule_and_full_cascade(monkeypatch):
    parent_id, child_id = "parent-page", "child-page"
    base_operation, split_operation = "seed-parent", "split-child"
    base = {
        "schema_version": 1, "operation_id": base_operation, "document_id": "doc_seed",
        "page_id": parent_id,
        "concept": {
            "slug": "parent", "title": "Parent", "definition": "Parent evidence",
            "identity_aliases": [], "anchor_reference_ids": ["doc_seed:B1"],
            "evidence_claim_ids": [f"{base_operation}:evidence"], "source_document_ids": ["doc_seed"],
        },
        "evidence_units": [{
            "evidence_id": f"{base_operation}:evidence", "claim": "Parent evidence",
            "anchor_reference_ids": ["doc_seed:B1"], "source_document_id": "doc_seed",
        }],
        "source_key_points": [{"text": "Parent evidence", "anchor_reference_ids": ["doc_seed:B1"]}],
        "source_blocks": [], "links": [],
        "entry_changes": [{
            "op": "add", "entry_id": "entry-child", "name": "Child", "definition": "Child detail",
            "parent_entry_id": None, "anchor_reference_ids": ["doc_seed:B1"],
            "evidence_claim_ids": [f"{base_operation}:evidence"],
        }],
    }
    reference = {
        "source": "concept:parent", "target": "concept:child", "relation": "entry_reference",
        "entry_id": "entry-child", "name": "Child", "parent_entry_id": None,
    }
    parent_split = {
        "schema_version": 1, "artifact_type": "lint", "operation_id": split_operation,
        "document_id": f"lint:{split_operation}", "page_id": parent_id, "content_action": "none",
        "concept": {"slug": "parent", "title": "Parent", "definition": "", "aliases": [],
                    "source_document_ids": [], "evidence_claim_ids": []},
        "evidence_units": [], "source_blocks": [], "source_key_points": [],
        "added_links": [reference], "removed_links": [],
        "entry_changes": [{"op": "remove", "entry_id": "entry-child"}],
    }
    child_linked = {
        "schema_version": 1, "operation_id": base_operation, "document_id": "doc_seed", "page_id": child_id,
        "entry_changes": [base["entry_changes"][0]], "evidence_units": base["evidence_units"],
        "source_blocks": [], "links": [],
    }
    child_split = {
        "schema_version": 1, "artifact_type": "lint", "operation_id": split_operation,
        "document_id": f"lint:{split_operation}", "page_id": child_id, "content_action": "create",
        "concept": {"slug": "child", "title": "Child", "definition": "Parent에서 나눈 하위 항목이다.",
                    "aliases": [], "source_document_ids": [], "evidence_claim_ids": []},
        "evidence_units": [], "source_blocks": [], "source_key_points": [],
        "added_links": [], "removed_links": [],
        "split_from": {"page_id": parent_id, "slug": "parent", "entry_id": "entry-child"},
        "linked_operation_ids": [base_operation],
    }
    by_page = {
        parent_id: [base, parent_split],
        child_id: [child_linked, child_split],
    }
    ledger = []
    snapshots = []
    with _connect() as conn:
        for page_id, slug in ((parent_id, "parent"), (child_id, "child")):
            contributions = by_page[page_id]
            for sequence, contribution in enumerate(contributions, 1):
                operation_id = contribution["operation_id"]
                key = f"wiki/workspace-1/pages/{page_id}/ops/{operation_id}.json"
                write_text_object(key, json.dumps(contribution))
                ledger.append({
                    "page_id": page_id, "ingest_operation_id": operation_id,
                    "source_document_id": contribution.get("document_id"), "sequence_revision": sequence,
                    "active": True, "object_key": key, "lint_operation_id": None,
                })
            rebuilt = rebuild_concept_page([
                PageContribution(page_id, "concept", item["operation_id"], sequence, "", item)
                for sequence, item in enumerate(contributions, 1)
            ])
            uri = write_text_object(f"baseline/{page_id}.md", rebuilt.markdown)
            conn.execute(
                "INSERT INTO wiki_pages(id,page_type,title,slug,summary,markdown_uri,user_id,workspace_id,status,created_at,updated_at) "
                "VALUES (%s,'concept',%s,%s,'',%s,'user-1','workspace-1','active',now(),now())",
                (page_id, slug.title(), slug, uri),
            )
        conn.execute(
            "INSERT INTO wiki_page_entries(workspace_id,entry_id,page_id,name,parent_entry_id,definition,block_refs,slugs,aliases,tags) "
            "VALUES ('workspace-1','entry-child',%s,'Child',NULL,'Child detail',ARRAY['doc_seed:B1'],ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[])",
            (child_id,),
        )
        conn.execute(
            "INSERT INTO wiki_page_entry_references(page_id,entry_id,workspace_id,target_slug,name,parent_entry_id) "
            "VALUES (%s,'entry-child','workspace-1','child','Child',NULL)",
            (parent_id,),
        )
        conn.execute(
            "INSERT INTO wiki_page_links(from_page_id,to_page_id,link_type,workspace_id,created_at,updated_at) "
            "VALUES (%s,%s,'entry_reference','workspace-1',now(),now())",
            (parent_id, child_id),
        )
        for page_id in (parent_id, child_id):
            snapshot = calculation._page_snapshot(dict(conn.execute(
                "SELECT * FROM wiki_pages WHERE id=%s", (page_id,),
            ).fetchone()))
            snapshots.append({key: snapshot[key] for key in ("page_id", "version", "content_hash")})

    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: ledger)
    source = Source([("B0001", "Controlled lint context.")])
    grant = str(uuid4())
    payload, _snapshot = _lint_payload(
        grant, snapshots, [{
            "operation_id": "merge-child", "kind": "MERGE_CONCEPTS", "target_page_id": parent_id,
            "depends_on": [], "arguments": {
                "source_page_ids": [child_id], "destination_page_id": parent_id,
            },
        }], source,
    )
    result = _intake(source).lint(payload)
    preview = _intake(source).preview(result["draft_id"], 1, "workspace-1", "user-1")
    assert any(item["kind"] == "PAGE_DELETE" and item["page_id"] == child_id for item in preview["changes"])
    assert any(item["kind"] == "ENTRY" and item["action"] == "ADD" and item["page_id"] == parent_id for item in preview["changes"])
    assert any(item["kind"] == "REFERENCE" and item["action"] == "REMOVE" for item in preview["changes"])

    apply_payload = _apply_payload(grant, result, operation_id="document-lint-merge-operation")
    published = McpWikiDraftApply(
        PostgresMcpWikiApplyRepository(), _ValidApprovalAuthority()
    ).apply(apply_payload.draft_id, apply_payload)
    assert published["state"] == "PUBLISHED"
    with _connect() as conn:
        assert conn.execute("SELECT status FROM wiki_pages WHERE id=%s", (child_id,)).fetchone()["status"] == "deleted"
        assert conn.execute(
            "SELECT count(*) AS count FROM wiki_page_entries WHERE page_id=%s AND entry_id='entry-child'",
            (parent_id,),
        ).fetchone()["count"] == 1
        pending = conn.execute(
            "SELECT operation_id,linked_by_operation_id FROM wiki_pending_contributions"
        ).fetchall()
    assert len(pending) == 2
    assert all(row == {"operation_id": apply_payload.operation_id, "linked_by_operation_id": None} for row in pending)


def test_lint_expiry_and_purge_remove_private_snapshot_and_fence_late_commands(monkeypatch):
    ledger, target, _statement = _seed_lint_page("alpha-page", "alpha")
    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: [ledger])
    source = Source([("B0001", "Alpha additional evidence.")])
    grant = str(uuid4())

    def payload(key: str, operation_id: str):
        return _lint_payload(
            grant, [target], lambda snapshot: [{
                "operation_id": operation_id, "kind": "UPSERT_STATEMENT",
                "target_page_id": "alpha-page", "depends_on": [],
                "arguments": {
                    "statement_id": None, "text": "Alpha additional evidence.",
                    "evidence": [{
                        "snapshot_id": snapshot, "block_id": "B0001", "quote": "Alpha additional evidence.",
                    }],
                },
            }], source, key=key,
        )[0]

    intake = _intake(source)
    expired = intake.lint(payload("lint-expire", "add-expired"))
    with _connect() as conn:
        conn.execute("UPDATE mcp_wiki_drafts SET expires_at=now()-interval '1 second' WHERE draft_id=%s", (expired["draft_id"],))
    assert expire_inactive_runs() == 1
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_drafts WHERE draft_id=%s", (expired["draft_id"],)).fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_lint_source_blocks WHERE draft_id=%s", (expired["draft_id"],)).fetchone()["count"] == 0
        assert conn.execute("SELECT status FROM ai_task_runs WHERE id=%s", (expired["draft_id"],)).fetchone()["status"] == "cancelled"

    active_payload = payload("lint-purge", "add-purged")
    active = intake.lint(active_payload)
    deleted = postgres_purge.purge_user("user-1")
    assert deleted["mcp_wiki_lint_drafts"] == 1
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_drafts WHERE draft_id=%s", (active["draft_id"],)).fetchone()["count"] == 0
    with pytest.raises(McpWikiDraftError) as purged:
        intake.lint(active_payload)
    assert purged.value.code == "PERMISSION_REVOKED"


def test_lint_fails_closed_on_source_quote_target_and_ledger_drift(monkeypatch):
    ledger, target, _statement = _seed_lint_page("alpha-page", "alpha")
    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: [ledger])
    grant = str(uuid4())

    def upsert(source: Source, frozen_target=target, quote="Alpha exact evidence.", key="lint-drift"):
        return _lint_payload(
            grant, [frozen_target], lambda snapshot: [{
                "operation_id": "add-alpha", "kind": "UPSERT_STATEMENT",
                "target_page_id": "alpha-page", "depends_on": [],
                "arguments": {
                    "statement_id": None, "text": "Alpha exact evidence.",
                    "evidence": [{"snapshot_id": snapshot, "block_id": "B0001", "quote": quote}],
                },
            }], source, key=key,
        )[0]

    source = Source([("B0001", "Alpha exact evidence.")])
    drifted = upsert(source)
    source.blocks = [("B0001", "Alpha changed after mapping.")]
    with pytest.raises(McpWikiDraftError) as source_changed:
        _intake(source).lint(drifted)
    assert source_changed.value.code == "SOURCE_CHANGED"

    source = Source([("B0001", "Alpha exact evidence.")])
    with pytest.raises(McpWikiDraftError) as bad_quote:
        _intake(source).lint(upsert(source, quote="not an exact quote", key="lint-quote"))
    assert bad_quote.value.code == "INVALID_EVIDENCE"

    changed_target = {**target, "content_hash": "sha256:" + "f" * 64}
    with pytest.raises(McpWikiDraftError) as target_changed:
        _intake(source).lint(upsert(source, frozen_target=changed_target, key="lint-target"))
    assert target_changed.value.code == "TARGET_CHANGED"

    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: [])
    with pytest.raises(McpWikiDraftError) as ledger_pending:
        _intake(source).lint(upsert(source, key="lint-ledger"))
    assert ledger_pending.value.code == "LEDGER_RECONCILIATION_PENDING"


def test_actual_route_persists_immutable_snapshot_and_repeated_window(monkeypatch):
    grant = str(uuid4())
    source = Source()
    intake = _intake(source)
    api.app.dependency_overrides[get_mcp_wiki_draft_intake] = lambda: intake
    payload = _start_payload(grant)
    with TestClient(api.app) as client:
        response = client.post(
            "/internal/mcp/wiki/ingest-runs",
            headers={"X-Agent-Service-Token": "agent-test-token"},
            content=payload.model_dump_json(),
        )
        assert response.status_code == 200
        started = response.json()
        get_actor = _actor("get_window", grant)
        body = {"schema_version": "mcp.wiki-draft.v1", "actor": get_actor, "window_ordinal": 1}
        first = client.post(
            f"/internal/mcp/wiki/ingest-runs/{started['run_id']}/windows/read",
            headers={"X-Agent-Service-Token": "agent-test-token"}, json=body,
        )
        body["actor"]["invocation_id"] = str(uuid4())
        second = client.post(
            f"/internal/mcp/wiki/ingest-runs/{started['run_id']}/windows/read",
            headers={"X-Agent-Service-Token": "agent-test-token"}, json=body,
        )
    assert source.calls == 1
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["data_boundary"] == "UNTRUSTED_WORKSPACE_CONTENT"
    with _connect() as conn:
        stored = conn.execute("SELECT text,content_hash FROM mcp_wiki_source_blocks ORDER BY ordinal").fetchall()
    assert [row["text"] for row in stored] == [text for _, text in source.blocks]
    assert first.json()["window_hash"] == canonical_hash({
        "snapshot_id": started["source_snapshot"]["snapshot_id"], "window_ordinal": 1,
        "blocks": [{"block_id": "B0001", "ordinal": 1, "content_hash": stored[0]["content_hash"]}],
    })


def test_agent_auth_precedes_body_validation_and_unknown_fields_are_rejected():
    grant = str(uuid4())
    payload = _start_payload(grant).model_dump(mode="json") | {"source_version": 9}
    api.app.dependency_overrides[get_mcp_wiki_draft_intake] = _intake
    with TestClient(api.app) as client:
        unauthenticated = client.post("/internal/mcp/wiki/ingest-runs", content=b"not-json")
        invalid = client.post(
            "/internal/mcp/wiki/ingest-runs",
            headers={"X-Agent-Service-Token": "agent-test-token"}, json=payload,
        )
    assert unauthenticated.status_code == 401
    assert invalid.status_code == 400
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_ingest_runs").fetchone()["count"] == 0


def test_concurrent_start_idempotency_and_cross_owner_not_found():
    grant = str(uuid4())
    source = Source()
    intake = _intake(source)
    payload = _start_payload(grant)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: intake.start(payload), range(2)))
    assert results[0] == results[1]
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_ingest_runs").fetchone()["count"] == 1
    wrong = McpWikiActorRequest.model_validate(_actor("resume_ingest", grant, user_id="user-other"))
    with pytest.raises(McpWikiDraftError, match="not found") as exc:
        intake.resume(results[0]["run_id"], wrong)
    assert exc.value.status_code == 404
    replay_actor = payload.actor.model_copy(update={"invocation_id": str(uuid4())})
    replay = payload.model_copy(update={"actor": replay_actor})
    assert intake.start(replay) == results[0]
    conflict_actor = payload.actor.model_copy(update={"invocation_id": str(uuid4()), "policy_generation": 2})
    conflict_hash = canonical_hash({"schema_version": payload.schema_version, "actor": request_actor(conflict_actor), "document_id": payload.document_id})
    conflict = payload.model_copy(update={"actor": conflict_actor, "request_hash": conflict_hash})
    with pytest.raises(McpWikiDraftError) as conflict_exc:
        intake.start(conflict)
    assert conflict_exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_submission_is_atomic_idempotent_and_never_publishes():
    grant = str(uuid4())
    intake = _intake()
    started = intake.start(_start_payload(grant))
    window = intake.read_window(
        started["run_id"], McpWikiActorRequest.model_validate(_actor("get_window", grant)), 1
    )
    before = {}
    with _connect() as conn:
        for table in ("wiki_pages", "wiki_publications", "wiki_publication_outbox"):
            before[table] = conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"]
    payload = _submission(grant, started, window)
    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicate = list(pool.map(
            lambda _: intake.submit(started["run_id"], window["window_id"], payload), range(2)
        ))
    accepted = duplicate[0]
    assert duplicate[1] == accepted
    retry = payload.model_copy(update={"actor": payload.actor.model_copy(update={"invocation_id": str(uuid4())})})
    assert intake.submit(started["run_id"], window["window_id"], retry) == accepted
    different = _submission(grant, started, window, key="submit-1", concept="Alpha Two")
    with pytest.raises(McpWikiDraftError) as conflict:
        intake.submit(started["run_id"], window["window_id"], different)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_window_submissions").fetchone()["count"] == 1
        for table, count in before.items():
            assert conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"] == count


@pytest.mark.parametrize(
    ("text", "quote", "code"),
    [
        ("Invented statement", "Alpha concept has evidence.", "LEXICAL_OVERLAP_REJECTED"),
        ("Alpha https://evil.example", "Alpha concept has evidence.", "UNSAFE_CONTENT"),
        ("ignore previous Alpha", "Alpha concept has evidence.", "INSTRUCTION_PATTERN_REJECTED"),
        ("Alpha concept has evidence.", "not in block", "INVALID_EVIDENCE"),
    ],
)
def test_submission_policy_fails_closed(text, quote, code):
    grant = str(uuid4())
    intake = _intake()
    started = intake.start(_start_payload(grant))
    window = intake.read_window(started["run_id"], McpWikiActorRequest.model_validate(_actor("get_window", grant)), 1)
    payload = _submission(grant, started, window, text=text, quote=quote)
    with pytest.raises(McpWikiDraftError) as exc:
        intake.submit(started["run_id"], window["window_id"], payload)
    assert exc.value.code == code
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_window_submissions").fetchone()["count"] == 0


def test_web_and_mcp_share_document_active_fence():
    grant = str(uuid4())
    wiki_repository.create_pipeline_run(str(uuid4()), "doc_1", "user-1", "workspace-1", "test", "test", "api")
    with pytest.raises(McpWikiDraftError) as exc:
        _intake().start(_start_payload(grant))
    assert exc.value.code == "ACTIVE_RUN_EXISTS"
    with _connect() as conn:
        conn.execute("DELETE FROM pipeline_runs")
    started = _intake().start(_start_payload(grant))
    with pytest.raises(ActiveIngestRunError):
        wiki_repository.create_pipeline_run(str(uuid4()), "doc_1", "user-1", "workspace-1", "test", "test", "api")
    assert started["state"] == "ACTIVE_WINDOWING"


def test_composite_snapshot_locator_keeps_same_block_id_distinct_across_documents():
    grant = str(uuid4())
    intake = _intake(Source([("B0001", "Alpha concept has evidence.")]))
    first = intake.start(_start_payload(grant, document_id="doc_1", key="one"))
    second = intake.start(_start_payload(grant, document_id="doc_2", key="two"))
    assert first["source_snapshot"]["snapshot_id"] != second["source_snapshot"]["snapshot_id"]
    with _connect() as conn:
        rows = conn.execute("SELECT snapshot_id,block_id FROM mcp_wiki_source_blocks ORDER BY snapshot_id").fetchall()
    assert len(rows) == 2
    assert {row["block_id"] for row in rows} == {"B0001"}
    assert len({row["snapshot_id"] for row in rows}) == 2


def test_finalize_is_domain_idempotent_and_preview_is_owner_scoped():
    grant = str(uuid4())
    intake = _intake()
    started = _complete_intake(intake, grant)
    first = _finalize_payload(grant, started, key="finalize-a")
    second = _finalize_payload(grant, started, key="finalize-b")
    wrong_owner = first.model_copy(update={
        "actor": first.actor.model_copy(update={"invocation_id": str(uuid4()), "user_id": "user-other"})
    })
    with pytest.raises(McpWikiDraftError) as hidden_finalize:
        intake.finalize(started["run_id"], wrong_owner)
    assert hidden_finalize.value.code == "NOT_FOUND"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda payload: intake.finalize(started["run_id"], payload),
            (first, second),
        ))
    assert results[0] == results[1]
    assert results[0]["state"] == "AWAITING_WEB_APPROVAL"
    preview = intake.preview(started["draft_id"], 1, "workspace-1", "user-1")
    assert preview["schema_version"] == "mcp.wiki-preview.v1"
    assert preview["complete"] is True
    assert {item["kind"] for item in preview["changes"]} >= {"SOURCE_PAGE", "CONCEPT_STATEMENT"}
    assert preview["draft_hash"] == results[0]["draft_hash"]
    changed_intent = _finalize_payload(grant, started, key="finalize-c", count=1)
    with pytest.raises(McpWikiDraftError) as conflict:
        intake.finalize(started["run_id"], changed_intent)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(McpWikiDraftError) as wrong_owner:
        intake.preview(started["draft_id"], 1, "workspace-1", "user-other")
    assert wrong_owner.value.code == "NOT_FOUND"
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_drafts").fetchone()["count"] == 1
        assert conn.execute("SELECT state FROM mcp_wiki_ingest_runs WHERE run_id=%s", (started["run_id"],)).fetchone()["state"] == "AWAITING_WEB_APPROVAL"
        assert conn.execute("SELECT count(*) AS count FROM wiki_pages").fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM wiki_publications").fetchone()["count"] == 0

    api.app.dependency_overrides[get_mcp_wiki_draft_intake] = lambda: intake
    with TestClient(api.app) as client:
        response = client.get(
            f"/internal/mcp/wiki/drafts/{started['draft_id']}/preview?draft_version=1",
            headers={
                "X-Agent-Service-Token": "agent-test-token",
                "X-MCP-Workspace-Id": "workspace-1",
                "X-MCP-User-Id": "user-1",
            },
        )
        hidden = client.get(
            f"/internal/mcp/wiki/drafts/{started['draft_id']}/preview?draft_version=1",
            headers={
                "X-Agent-Service-Token": "agent-test-token",
                "X-MCP-Workspace-Id": "workspace-1",
                "X-MCP-User-Id": "user-other",
            },
        )
    assert response.status_code == 200 and response.json() == preview
    assert hidden.status_code == 404


def test_explicit_apply_publishes_once_with_atomic_body_free_result_and_outboxes():
    grant = str(uuid4())
    intake = _intake()
    started = _complete_intake(intake, grant)
    finalized = intake.finalize(started["run_id"], _finalize_payload(grant, started))
    payload = _apply_payload(grant, finalized)
    apply = McpWikiDraftApply(PostgresMcpWikiApplyRepository(), _ValidApprovalAuthority())
    with _connect() as conn:
        source_hash = conn.execute(
            "SELECT source_content_hash FROM mcp_wiki_ingest_runs WHERE run_id=%s", (started["run_id"],)
        ).fetchone()["source_content_hash"]
        stored_source_hash = source_hash.removeprefix("sha256:")
        conn.execute(
            "INSERT INTO document_derived_state(document_id,workspace_id,last_edit_revision,last_edit_hash,last_edited_at) "
            "VALUES ('doc_1','workspace-1',7,%s,now())",
            (stored_source_hash,),
        )

    first = apply.apply(payload.draft_id, payload)
    retry = payload.model_copy(update={
        "actor": payload.actor.model_copy(update={"invocation_id": str(uuid4())})
    })
    second = apply.apply(payload.draft_id, retry)
    assert first == second
    assert first["state"] == "PUBLISHED"
    assert first["changed"] is True
    assert first["error_code"] is None

    with _connect() as conn:
        result = conn.execute(
            "SELECT * FROM mcp_wiki_publication_results WHERE approval_id=%s",
            (payload.approval_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT payload FROM mcp_wiki_publication_outbox WHERE approval_id=%s",
            (payload.approval_id,),
        ).fetchone()["payload"]
        assert result["state"] == "PUBLISHED"
        assert conn.execute("SELECT count(*) AS count FROM wiki_pages WHERE status='active'").fetchone()["count"] == 3
        assert conn.execute("SELECT count(*) AS count FROM wiki_publications WHERE state='COMMITTED'").fetchone()["count"] == 1
        assert conn.execute("SELECT count(*) AS count FROM wiki_publication_outbox").fetchone()["count"] == 1
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_publication_outbox").fetchone()["count"] == 1
        assert conn.execute("SELECT status FROM ai_task_runs WHERE id=%s", (started["run_id"],)).fetchone()["status"] == "completed"
        assert conn.execute(
            "SELECT ingested_hash FROM document_derived_state WHERE document_id='doc_1'"
        ).fetchone()["ingested_hash"] == stored_source_hash
        conn.execute(
            "UPDATE document_derived_state SET last_edit_revision=8,last_edit_hash=%s,last_edited_at=now() "
            "WHERE document_id='doc_1'",
            ("f" * 64,),
        )
        derived = conn.execute(
            "SELECT ingested_hash,last_edit_hash FROM document_derived_state WHERE document_id='doc_1'"
        ).fetchone()
        assert derived["ingested_hash"] == stored_source_hash
        assert derived["last_edit_hash"] != derived["ingested_hash"]
    assert set(event) == {
        "event_id", "event_type", "approval_id", "operation_id", "draft_id", "workspace_id",
        "user_id", "grant_id", "client_id", "kind", "outcome", "changed",
        "published_reference_hash", "committed_at",
    }
    assert event["event_type"] == "mcp.wiki.publication.v1"
    assert event["outcome"] == "PUBLISHED"
    assert event["published_reference_hash"] == first["published_reference_hash"]

    hidden = apply.result(payload.approval_id, payload.operation_id, "workspace-1", "user-other")
    assert hidden["state"] == "NOT_FOUND"
    cross_pair = payload.model_copy(update={
        "actor": payload.actor.model_copy(update={"invocation_id": str(uuid4())}),
        "operation_id": "document-apply-operation-2",
    })
    with pytest.raises(McpWikiDraftError) as conflict:
        apply.apply(payload.draft_id, cross_pair)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_apply_draft_version_rejects_a_different_approval_operation_pair():
    grant = str(uuid4())
    intake = _intake()
    started = _complete_intake(intake, grant)
    finalized = intake.finalize(started["run_id"], _finalize_payload(grant, started))
    first = _apply_payload(grant, finalized)
    apply = McpWikiDraftApply(PostgresMcpWikiApplyRepository(), _UnknownApprovalAuthority())
    assert apply.apply(first.draft_id, first)["state"] == "UNKNOWN"

    other = first.model_copy(update={
        "approval_id": str(uuid4()),
        "operation_id": "document-apply-operation-2",
        "actor": first.actor.model_copy(update={"invocation_id": str(uuid4())}),
    })
    with pytest.raises(McpWikiDraftError) as conflict:
        apply.apply(other.draft_id, other)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_apply_public_state_recheck_conflicts_before_any_public_effect(monkeypatch):
    grant = str(uuid4())
    intake = _intake()
    started = _complete_intake(intake, grant)
    finalized = intake.finalize(started["run_id"], _finalize_payload(grant, started))
    payload = _apply_payload(grant, finalized)
    real_publish = apply_repository_module.publish_ingest

    def changed_state(*args, **kwargs):
        calculation_result, page_ids, manifest = real_publish(*args, **kwargs)
        calculation_result["preview"] = {**calculation_result["preview"], "public_state_hash": "sha256:" + "f" * 64}
        return calculation_result, page_ids, manifest

    monkeypatch.setattr(apply_repository_module, "publish_ingest", changed_state)
    result = McpWikiDraftApply(
        PostgresMcpWikiApplyRepository(), _ValidApprovalAuthority()
    ).apply(payload.draft_id, payload)
    assert result["state"] == "CONFLICTED"
    assert result["error_code"] == "PUBLIC_STATE_CHANGED"
    with _connect() as conn:
        # 웹 반영을 실제로 실행한 뒤 해시를 맞추므로 savepoint가 공개 상태를 되돌린다. 발행 기록은 준비만 남는다.
        assert conn.execute("SELECT count(*) AS count FROM wiki_pages").fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM wiki_pending_contributions").fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM wiki_publications WHERE state<>'PREPARED'").fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_publication_outbox").fetchone()["count"] == 0


def test_source_edit_event_after_authority_check_remains_stale_after_publish():
    grant = str(uuid4())
    intake = _intake()
    started = _complete_intake(intake, grant)
    finalized = intake.finalize(started["run_id"], _finalize_payload(grant, started))
    payload = _apply_payload(grant, finalized, operation_id="document-source-race")
    with _connect() as conn:
        source_hash = conn.execute(
            "SELECT source_content_hash FROM mcp_wiki_ingest_runs WHERE run_id=%s", (started["run_id"],)
        ).fetchone()["source_content_hash"].removeprefix("sha256:")
        conn.execute(
            "INSERT INTO document_derived_state(document_id,workspace_id,last_edit_revision,last_edit_hash,last_edited_at) "
            "VALUES ('doc_1','workspace-1',7,%s,now())",
            (source_hash,),
        )

    class EditAfterAuthority:
        def lookup(self, request):
            with _connect() as conn:
                conn.execute(
                    "UPDATE document_derived_state SET last_edit_revision=8,last_edit_hash=%s,last_edited_at=now() "
                    "WHERE document_id='doc_1'",
                    ("f" * 64,),
                )
            return {"schema_version": "mcp.wiki-approval-status.v1", "status": "VALID", "reason": None}

    result = McpWikiDraftApply(
        PostgresMcpWikiApplyRepository(), EditAfterAuthority()
    ).apply(payload.draft_id, payload)
    assert result["state"] == "PUBLISHED"
    with _connect() as conn:
        derived = conn.execute(
            "SELECT ingested_hash,last_edit_hash FROM document_derived_state WHERE document_id='doc_1'"
        ).fetchone()
    assert derived == {"ingested_hash": None, "last_edit_hash": "f" * 64}


def test_apply_upload_failure_keeps_old_public_state_and_same_pair_retry_commits(monkeypatch):
    grant = str(uuid4())
    intake = _intake()
    started = _complete_intake(intake, grant)
    finalized = intake.finalize(started["run_id"], _finalize_payload(grant, started))
    payload = _apply_payload(grant, finalized)
    apply = McpWikiDraftApply(PostgresMcpWikiApplyRepository(), _ValidApprovalAuthority())
    real_write = apply_repository_module.write_publication_text

    monkeypatch.setattr(
        apply_repository_module,
        "write_publication_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("controlled upload failure")),
    )
    with pytest.raises(OSError, match="controlled upload failure"):
        apply.apply(payload.draft_id, payload)
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM wiki_pages").fetchone()["count"] == 0
        assert conn.execute("SELECT state FROM mcp_wiki_publication_results").fetchone()["state"] == "APPLYING"
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_publication_outbox").fetchone()["count"] == 0
        assert conn.execute("SELECT state FROM wiki_publications").fetchone()["state"] == "PREPARED"

    monkeypatch.setattr(apply_repository_module, "write_publication_text", real_write)
    retry = payload.model_copy(update={
        "actor": payload.actor.model_copy(update={"invocation_id": str(uuid4())})
    })
    result = apply.apply(payload.draft_id, retry)
    assert result["state"] == "PUBLISHED"
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM wiki_publication_outbox").fetchone()["count"] == 1
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_publication_outbox").fetchone()["count"] == 1


@pytest.mark.parametrize("target_deleted", [False, True])
def test_client_semantic_merge_survives_preview_and_apply(monkeypatch, target_deleted):
    ledger, _, _ = _seed_lint_page("auth-page", "authentication")
    monkeypatch.setattr(calculation, "read_contributions", lambda *_args: [ledger])
    grant = str(uuid4())
    intake = _intake()
    started = intake.start(_start_payload(grant))
    for ordinal, name, text in (
        (1, "Login", "Alpha concept has evidence."),
        (2, "Sign in", "Beta concept has support."),
    ):
        window = intake.read_window(started["run_id"], McpWikiActorRequest.model_validate(_actor("get_window", grant)), ordinal)
        intake.submit(started["run_id"], window["window_id"], _submission(
            grant, started, window, key=f"submit-{ordinal}", concept=name, text=text, quote=text,
            block_id=f"B000{ordinal}", merge_target="auth-page",
        ))
    finalized = intake.finalize(started["run_id"], _finalize_payload(grant, started))
    preview = intake.preview(started["draft_id"], 1, "workspace-1", "user-1")
    statements = [item for item in preview["changes"] if item["kind"] == "CONCEPT_STATEMENT" and item["action"] == "ADD"]
    assert len(statements) == 2
    assert {item["page_id"] for item in statements} == {"auth-page"}
    with _connect() as conn:
        conn.execute(
            "INSERT INTO document_derived_state(document_id,workspace_id,last_edit_revision,last_edit_hash,last_edited_at) "
            "VALUES ('doc_1','workspace-1',7,%s,now())",
            (started["source_snapshot"]["source_content_hash"].removeprefix("sha256:"),),
        )
        if target_deleted:
            conn.execute("UPDATE wiki_pages SET status='deleted',updated_at=now() WHERE id='auth-page'")
    payload = _apply_payload(grant, finalized, operation_id=f"semantic-merge-{uuid4()}")
    result = McpWikiDraftApply(PostgresMcpWikiApplyRepository(), _ValidApprovalAuthority()).apply(payload.draft_id, payload)
    assert result["state"] == ("CONFLICTED" if target_deleted else "PUBLISHED")
    with _connect() as conn:
        concepts = conn.execute("SELECT id,title,markdown_uri FROM wiki_pages WHERE page_type='concept'").fetchall()
    assert len(concepts) == 1 and concepts[0]["id"] == "auth-page"
    assert concepts[0]["title"] == "Authentication"
    if not target_deleted:
        body = read_text_object(concepts[0]["markdown_uri"])
        assert "Alpha concept has evidence." in body and "Beta concept has support." in body


def test_finalize_combines_same_concept_and_local_statement_id_across_windows():
    grant = str(uuid4())
    intake = _intake(Source([
        ("B0001", "Alpha first evidence."),
        ("B0002", "Alpha second evidence."),
    ]))
    started = intake.start(_start_payload(grant))
    first = intake.read_window(
        started["run_id"], McpWikiActorRequest.model_validate(_actor("get_window", grant)), 1,
    )
    intake.submit(started["run_id"], first["window_id"], _submission(
        grant, started, first, text="Alpha first evidence.", quote="Alpha first evidence.",
    ))
    second = intake.read_window(
        started["run_id"], McpWikiActorRequest.model_validate(_actor("get_window", grant)), 2,
    )
    intake.submit(started["run_id"], second["window_id"], _submission(
        grant, started, second, key="submit-2", text="Alpha second evidence.",
        quote="Alpha second evidence.", block_id="B0002",
    ))
    intake.finalize(started["run_id"], _finalize_payload(grant, started))
    preview = intake.preview(started["draft_id"], 1, "workspace-1", "user-1")
    statements = [item for item in preview["changes"] if item["kind"] == "CONCEPT_STATEMENT"]
    assert len(statements) == 2
    assert len({item["page_id"] for item in statements}) == 1
    assert {item["subject_id"] for item in statements} == {"1:statement-1", "2:statement-1"}
    with _connect() as conn:
        inputs = conn.execute(
            "SELECT calculation_inputs FROM mcp_wiki_drafts WHERE draft_id=%s", (started["draft_id"],),
        ).fetchone()["calculation_inputs"]
    concept_pages = [key for key, _value in inputs["effect"] if key[0] == "page" and key[1].startswith("new:concept:")]
    assert concept_pages == [["page", "new:concept:alpha"]]


def test_full_preview_replays_invalidation_reference_cascade_and_unique_revival(monkeypatch):
    source_page, revived, stale, referring = "source-page", "alpha-deleted", "gamma-page", "ref-page"
    source_uri = write_text_object("baseline/source.md", "# Existing source\n")
    stale_uri = write_text_object("baseline/gamma.md", "# Gamma\n")
    referring_uri = write_text_object("baseline/ref.md", "# Ref\n")
    with _connect() as conn:
        conn.cursor().executemany(
            "INSERT INTO wiki_pages(id,page_type,title,slug,markdown_uri,user_id,workspace_id,status,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,'user-1','workspace-1',%s,now(),now())",
            [
                (source_page, "source", "Source", "doc-1", source_uri, "active"),
                (revived, "concept", "Alpha", "alpha", None, "deleted"),
                (stale, "concept", "Gamma", "gamma", stale_uri, "active"),
                (referring, "concept", "Ref", "ref", referring_uri, "active"),
            ],
        )
        conn.cursor().executemany(
            "INSERT INTO document_wiki_links(document_id,wiki_page_id,relation_type,workspace_id,created_at) "
            "VALUES ('doc_1',%s,%s,'workspace-1',now())",
            [(source_page, "source_of"), (stale, "extracted_concept")],
        )
        conn.execute(
            "INSERT INTO wiki_source_manual_links(from_page_id,to_page_id,present,workspace_id) "
            "VALUES (%s,%s,true,'workspace-1')", (source_page, revived),
        )
        conn.execute(
            "INSERT INTO wiki_page_entries(workspace_id,entry_id,page_id,name,definition,block_refs,slugs,aliases,tags) "
            "VALUES ('workspace-1','entry-gamma',%s,'Gamma item','',ARRAY['doc_1:B0001'],ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[])",
            (stale,),
        )
        conn.execute(
            "INSERT INTO wiki_page_entry_references(page_id,entry_id,workspace_id,target_slug,name,parent_entry_id) "
            "VALUES (%s,'entry-gamma','workspace-1','gamma','Gamma item',NULL)", (referring,),
        )
        conn.execute(
            "INSERT INTO wiki_page_links(from_page_id,to_page_id,link_type,workspace_id,created_at,updated_at) "
            "VALUES (%s,%s,'entry_reference','workspace-1',now(),now())", (referring, stale),
        )
        conn.cursor().executemany(
            "INSERT INTO source_blocks(document_id,block_id,text,position) VALUES ('doc_1',%s,%s,%s)",
            [("B0001", "Old Gamma evidence.", 1), ("B0002", "Beta concept has support.", 2)],
        )
    gamma_row = _stored_contribution(
        stale, "gamma", "gamma-old", "doc_1", "doc_1:B0001",
        entries=[{
            "op": "add", "entry_id": "entry-gamma", "name": "Gamma item", "definition": "",
            "anchor_reference_ids": ["doc_1:B0001"], "evidence_claim_ids": ["gamma-old:evidence"],
        }],
    )
    ref_row = _stored_contribution(
        referring, "ref", "ref-old", "doc_other", "doc_other:B9",
        links=[{
            "source": "concept:ref", "target": "concept:gamma", "relation": "entry_reference",
            "entry_id": "entry-gamma", "name": "Gamma item", "parent_entry_id": None,
        }],
    )
    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: [gamma_row, ref_row])
    source = Source()
    intake = _intake(source)
    grant = str(uuid4())
    started = _complete_intake(intake, grant)
    source.blocks = [("B0001", "This later source state must not replace the frozen snapshot.")]
    minio = storage_client()
    bucket = os.environ["S3_BUCKET"]
    objects_before = [(item.object_name, item.etag, item.size) for item in minio.list_objects(bucket, recursive=True)]
    with _connect() as conn:
        public_before = {
            table: conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"]
            for table in ("wiki_pages", "wiki_page_entries", "wiki_page_entry_references", "wiki_page_links", "wiki_publications", "wiki_publication_outbox", "ai_task_changes")
        }

    finalized = intake.finalize(started["run_id"], _finalize_payload(grant, started))
    preview = intake.preview(started["draft_id"], 1, "workspace-1", "user-1")
    assert source.calls == 1
    assert any(item["kind"] == "PAGE_DELETE" and item["page_id"] == stale for item in preview["changes"])
    assert any(item["kind"] == "REFERENCE" and item["action"] == "REMOVE" and item["page_id"] == referring for item in preview["changes"])
    assert any(item["kind"] == "REVIVAL_LINK" and item["page_id"] == revived for item in preview["changes"])
    assert any(
        item["kind"] == "LINK" and item["action"] == "ADD"
        and item["page_id"] == source_page and item["subject_id"] == f"source_mentions_concept:{revived}"
        for item in preview["changes"]
    )
    assert finalized["draft_hash"] == preview["draft_hash"]
    assert objects_before == [(item.object_name, item.etag, item.size) for item in minio.list_objects(bucket, recursive=True)]
    with _connect() as conn:
        assert public_before == {
            table: conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"]
            for table in public_before
        }
        run = dict(conn.execute("SELECT * FROM mcp_wiki_ingest_runs WHERE run_id=%s", (started["run_id"],)).fetchone())
        draft = conn.execute("SELECT normalized_submissions FROM mcp_wiki_drafts WHERE draft_id=%s", (started["draft_id"],)).fetchone()
        blocks = [dict(row) for row in conn.execute(
            "SELECT block_id,ordinal,text,content_hash FROM mcp_wiki_source_blocks WHERE snapshot_id=%s ORDER BY ordinal",
            (run["snapshot_id"],),
        ).fetchall()]
        same = calculation.preview_ingest(
            conn, run=run, submissions=list(draft["normalized_submissions"]), source_blocks=blocks,
        )["preview"]
        assert same["draft_hash"] == preview["draft_hash"]
        conn.execute(
            "INSERT INTO wiki_page_entries(workspace_id,entry_id,page_id,name,definition,block_refs,slugs,aliases,tags) "
            "VALUES ('workspace-1','baseline-drift',%s,'Drift','',ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[],ARRAY[]::text[])",
            (referring,),
        )
        changed = calculation.preview_ingest(
            conn, run=run, submissions=list(draft["normalized_submissions"]), source_blocks=blocks,
        )["preview"]
        assert changed["public_state_hash"] != preview["public_state_hash"]
        assert changed["draft_hash"] != preview["draft_hash"]


def test_finalize_blocks_missing_active_contribution_ledger(monkeypatch):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,markdown_uri,user_id,workspace_id,status,created_at,updated_at) "
            "VALUES ('alpha-active','concept','Alpha','alpha',%s,'user-1','workspace-1','active',now(),now())",
            (write_text_object("baseline/alpha-active.md", "# Alpha\n"),),
        )
    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: [])
    grant = str(uuid4())
    intake = _intake()
    started = _complete_intake(intake, grant)
    with pytest.raises(McpWikiDraftError) as pending:
        intake.finalize(started["run_id"], _finalize_payload(grant, started))
    assert pending.value.code == "LEDGER_RECONCILIATION_PENDING"
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_drafts").fetchone()["count"] == 0


def test_finalize_blocks_contradictory_pending_contribution(monkeypatch):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,markdown_uri,user_id,workspace_id,status,created_at,updated_at) "
            "VALUES ('alpha-active','concept','Alpha','alpha',%s,'user-1','workspace-1','active',now(),now())",
            (write_text_object("baseline/alpha-active.md", "# Alpha\n"),),
        )
        conn.execute(
            "INSERT INTO wiki_pending_contributions(page_id,operation_id,workspace_id,document_id,object_key,active,sequence_revision,publication_id) "
            "VALUES ('alpha-active','prior-op','workspace-1','doc_prior','expected.json',true,1,'legacy')"
        )
    monkeypatch.setattr(calculation, "read_contributions", lambda page_ids, workspace_id: [{
        "page_id": "alpha-active", "ingest_operation_id": "prior-op",
        "source_document_id": "doc_prior", "sequence_revision": 1, "active": True,
        "object_key": "contradictory.json", "lint_operation_id": None,
    }])
    grant = str(uuid4())
    intake = _intake()
    started = _complete_intake(intake, grant)
    with pytest.raises(McpWikiDraftError) as pending:
        intake.finalize(started["run_id"], _finalize_payload(grant, started))
    assert pending.value.code == "LEDGER_RECONCILIATION_PENDING"
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_drafts").fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM wiki_pending_contributions").fetchone()["count"] == 1


@pytest.mark.parametrize(
    "limit_name",
    [
        "MCP_WIKI_DRAFT_PREVIEW_MAX_ITEMS",
        "MCP_WIKI_DRAFT_PREVIEW_MAX_BYTES",
        "MCP_WIKI_DRAFT_PREVIEW_MAX_SOURCE_BYTES",
    ],
)
def test_finalize_rejects_incomplete_windows_and_complete_limit(monkeypatch, limit_name):
    grant = str(uuid4())
    intake = _intake()
    started = intake.start(_start_payload(grant))
    window = intake.read_window(started["run_id"], McpWikiActorRequest.model_validate(_actor("get_window", grant)), 1)
    intake.submit(started["run_id"], window["window_id"], _submission(grant, started, window))
    with pytest.raises(McpWikiDraftError) as incomplete:
        intake.finalize(started["run_id"], _finalize_payload(grant, started))
    assert incomplete.value.code == "INCOMPLETE_WINDOWS"

    with _connect() as conn:
        conn.execute("TRUNCATE mcp_wiki_draft_idempotency,mcp_wiki_ingest_runs CASCADE")
    complete = _complete_intake(intake, grant)
    monkeypatch.setenv(limit_name, "1")
    with pytest.raises(McpWikiDraftError) as too_large:
        intake.finalize(complete["run_id"], _finalize_payload(grant, complete))
    assert too_large.value.code == "PREVIEW_TOO_LARGE"
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_drafts").fetchone()["count"] == 0
        assert conn.execute("SELECT state FROM mcp_wiki_ingest_runs WHERE run_id=%s", (complete["run_id"],)).fetchone()["state"] == "READY_TO_FINALIZE"


def test_finalized_submission_is_frozen_and_cancel_removes_preview():
    grant = str(uuid4())
    intake = _intake()
    started = _complete_intake(intake, grant)
    finalize = _finalize_payload(grant, started)
    intake.finalize(started["run_id"], finalize)
    with _connect() as conn:
        window = conn.execute("SELECT window_id,window_hash,blocks FROM mcp_wiki_windows WHERE run_id=%s ORDER BY window_ordinal LIMIT 1", (started["run_id"],)).fetchone()
    payload = _submission(grant, started, {
        "window_id": str(window["window_id"]), "window_hash": window["window_hash"]
    }, key="late-submit")
    with pytest.raises(McpWikiDraftError):
        intake.submit(started["run_id"], str(window["window_id"]), payload)
    postgres_task_journal.request_cancel(started["run_id"], "workspace-1", "user-1")
    postgres_task_journal.rollback_pending()
    with pytest.raises(McpWikiDraftError) as missing:
        intake.preview(started["draft_id"], 1, "workspace-1", "user-1")
    assert missing.value.code == "NOT_FOUND"
    with pytest.raises(McpWikiDraftError):
        intake.finalize(started["run_id"], finalize)
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_drafts").fetchone()["count"] == 0


def test_cancel_closes_unpublished_draft_without_wiki_changes():
    grant = str(uuid4())
    started = _intake().start(_start_payload(grant))
    postgres_task_journal.request_cancel(started["run_id"], "workspace-1", "user-1")
    postgres_task_journal.rollback_pending()
    with _connect() as conn:
        run = conn.execute("SELECT state FROM mcp_wiki_ingest_runs WHERE run_id=%s", (started["run_id"],)).fetchone()
        task = conn.execute("SELECT status FROM ai_task_runs WHERE id=%s", (started["run_id"],)).fetchone()
        assert conn.execute("SELECT count(*) AS count FROM wiki_publications").fetchone()["count"] == 0
    assert run["state"] == "CANCELLED"
    assert task["status"] == "cancelled"


def test_expiry_and_user_purge_do_not_recreate_draft():
    grant = str(uuid4())
    intake = _intake()
    started = _complete_intake(intake, grant)
    finalize = _finalize_payload(grant, started)
    intake.finalize(started["run_id"], finalize)
    with _connect() as conn:
        conn.execute("UPDATE mcp_wiki_ingest_runs SET expires_at=now()-interval '1 second'")
    assert expire_inactive_runs() == 1
    actor = McpWikiActorRequest.model_validate(_actor("resume_ingest", grant))
    with pytest.raises(McpWikiDraftError) as expired:
        intake.resume(started["run_id"], actor)
    assert expired.value.code == "EXPIRED"
    with pytest.raises(McpWikiDraftError):
        intake.finalize(started["run_id"], finalize)
    with pytest.raises(McpWikiDraftError) as expired_preview:
        intake.preview(started["draft_id"], 1, "workspace-1", "user-1")
    assert expired_preview.value.code == "NOT_FOUND"

    second = _complete_intake(intake, grant, start_key="start-2")
    second_finalize = _finalize_payload(grant, second, key="finalize-2")
    intake.finalize(second["run_id"], second_finalize)
    postgres_purge.purge_user("user-1")
    with pytest.raises(McpWikiDraftError) as purged:
        intake.resume(second["run_id"], actor)
    assert purged.value.code in {"PERMISSION_REVOKED", "NOT_FOUND"}
    with pytest.raises(McpWikiDraftError):
        intake.finalize(second["run_id"], second_finalize)
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_ingest_runs").fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM mcp_wiki_source_blocks").fetchone()["count"] == 0


@pytest.mark.parametrize("tool_name", ["start_ingest", "submit_lint_changes"])
def test_controlled_document_http_contract_is_exact(monkeypatch, tool_name):
    grant = str(uuid4())
    actor = McpWikiActorRequest.model_validate(_actor(tool_name, grant))
    text = "Alpha source"
    response = {
        "schema_version": "mcp.wiki-source.v1", "document_id": "doc_1", "display_name": "회의록",
        "workspace_id": "workspace-1", "source_version": 3, "source_content_hash": "sha256:" + sha256(text.encode()).hexdigest(),
        "mime_type": "text/plain", "blocks": [{
            "block_id": "B0001", "ordinal": 1, "text": text,
            "content_hash": "sha256:" + sha256(text.encode()).hexdigest(),
        }],
    }
    captured = {}

    class HttpResponse:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self, _limit):
            return json.dumps(response).encode()

    def open_request(request, timeout):
        captured.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data), timeout=timeout)
        return HttpResponse()

    monkeypatch.setattr(backend_source_reader, "urlopen", open_request)
    limits = __import__("app.modules.mcp_wiki_draft.application", fromlist=["load_intake_limits"]).load_intake_limits()
    result = BackendMcpWikiSourceReader("https://document.internal", "service-token", 7).read(actor, "doc_1", limits)
    assert result == response
    assert captured == {
        "url": "https://document.internal/internal/documents/doc_1/mcp-wiki-source",
        "headers": {"Content-type": "application/json", "X-agent-service-token": "service-token"},
        "body": {"schema_version": "mcp.wiki-source.v1", "actor": actor.model_dump()},
        "timeout": 7,
    }


@pytest.mark.parametrize("display_name", [
    None, "", "   ", "x" * 256, "제목\nsource_file: evil", "제목\r", "제목\t탭", "제목\x00", "제목\x85",
    "제목\u2028줄", "제목\u2029문단",
])
def test_document_source_display_name_is_required_bounded_untrusted_text(monkeypatch, display_name):
    actor = McpWikiActorRequest.model_validate(_actor("start_ingest", str(uuid4())))
    text = "Alpha source"
    response = {
        "schema_version": "mcp.wiki-source.v1", "document_id": "doc_1", "workspace_id": "workspace-1",
        "source_version": 3, "source_content_hash": "sha256:" + sha256(text.encode()).hexdigest(),
        "mime_type": "text/plain", "blocks": [{
            "block_id": "B0001", "ordinal": 1, "text": text,
            "content_hash": "sha256:" + sha256(text.encode()).hexdigest(),
        }],
    }
    if display_name is not None:
        response["display_name"] = display_name

    class HttpResponse:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self, _limit):
            return json.dumps(response).encode()

    monkeypatch.setattr(backend_source_reader, "urlopen", lambda _request, timeout: HttpResponse())
    limits = __import__("app.modules.mcp_wiki_draft.application", fromlist=["load_intake_limits"]).load_intake_limits()
    with pytest.raises(McpWikiDraftError) as rejected:
        BackendMcpWikiSourceReader("https://document.internal", "service-token", 7).read(actor, "doc_1", limits)
    assert rejected.value.code == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.parametrize("display_name", [
    "Screenshot 2026-11-11 at 2.09.40\u202fPM", "붙여넣은\u00a0이름", "Q3: plan # draft", "- 목록 같은 이름", "제목\u202e",
])
def test_document_source_display_name_accepts_names_document_accepts(monkeypatch, display_name):
    actor = McpWikiActorRequest.model_validate(_actor("start_ingest", str(uuid4())))
    text = "Alpha source"
    response = {
        "schema_version": "mcp.wiki-source.v1", "document_id": "doc_1", "display_name": display_name,
        "workspace_id": "workspace-1", "source_version": 3,
        "source_content_hash": "sha256:" + sha256(text.encode()).hexdigest(),
        "mime_type": "text/plain", "blocks": [{
            "block_id": "B0001", "ordinal": 1, "text": text,
            "content_hash": "sha256:" + sha256(text.encode()).hexdigest(),
        }],
    }

    class HttpResponse:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self, _limit):
            return json.dumps(response).encode()

    monkeypatch.setattr(backend_source_reader, "urlopen", lambda _request, timeout: HttpResponse())
    limits = __import__("app.modules.mcp_wiki_draft.application", fromlist=["load_intake_limits"]).load_intake_limits()
    result = BackendMcpWikiSourceReader("https://document.internal", "service-token", 7).read(actor, "doc_1", limits)
    assert result["display_name"] == display_name


@pytest.mark.parametrize("display_name", [
    "Screenshot 2026-11-11 at 2.09.40\u202fPM", "붙여넣은\u00a0이름", "Q3: plan # draft", "- 목록 같은 이름",
])
def test_source_page_renders_display_name_verbatim_with_safe_frontmatter(display_name):
    yaml = pytest.importorskip("yaml")
    markdown = calculation.build_ingest_manifest(
        run={"document_id": "doc_1", "source_display_name": display_name, "user_id": "user-1",
             "workspace_id": "workspace-1", "source_content_hash": "sha256:" + "0" * 64},
        submissions=[], source_blocks=[], current_source_blocks=[], concept_index=[], operation_id="operation",
    )["source_page"]["markdown"]
    assert f"\n# {display_name}\n" in markdown
    line = next(item for item in markdown.split("---\n")[1].splitlines() if item.startswith("source_file:"))
    assert yaml.safe_load(line) == {"source_file": display_name}


@pytest.mark.parametrize("existing_source_page", [False, True])
def test_source_page_title_is_document_display_name_not_document_id(monkeypatch, existing_source_page):
    # 웹 편입처럼 기존 소스 페이지의 수동 기여를 원장에서 찾는다. 이 문서에는 기여가 없다.
    monkeypatch.setattr(calculation, "read_contributions", lambda *_args: [])
    if existing_source_page:
        # 이전 MCP 편입이 문서 ID를 제목으로 남긴 소스 페이지를 재편입이 바로잡는다.
        # 예전 MCP slug(`doc_1`)는 스키마 적용이 웹 slug(`doc-1`)로 바꾼다(test_publication_schema_upgrade_review).
        uri = write_text_object("baseline/doc-1-source.md", "# doc_1\n")
        with _connect() as conn:
            conn.execute(
                "INSERT INTO wiki_pages(id,page_type,title,slug,markdown_uri,user_id,workspace_id,status,created_at,updated_at) "
                "VALUES ('source-page','source','doc_1','doc-1',%s,'user-1','workspace-1','active',now(),now())",
                (uri,),
            )
            conn.execute(
                "INSERT INTO document_wiki_links(document_id,wiki_page_id,relation_type,workspace_id,created_at) "
                "VALUES ('doc_1','source-page','source_of','workspace-1',now())"
            )
    grant = str(uuid4())
    intake = _intake(Source(display_name="회의록 2026-11"))
    started = _complete_intake(intake, grant)
    finalized = intake.finalize(started["run_id"], _finalize_payload(grant, started))
    preview = intake.preview(started["draft_id"], 1, "workspace-1", "user-1")
    source_change = next(item for item in preview["changes"] if item["kind"] == "SOURCE_PAGE")
    assert source_change["action"] == ("UPDATE" if existing_source_page else "ADD")
    assert "\n# 회의록 2026-11\n" in source_change["after_text"]
    assert 'source_file: "회의록 2026-11"\n' in source_change["after_text"]

    result = McpWikiDraftApply(PostgresMcpWikiApplyRepository(), _ValidApprovalAuthority()).apply(
        finalized["draft_id"], _apply_payload(grant, finalized)
    )
    assert result["state"] == "PUBLISHED"
    with _connect() as conn:
        page = conn.execute("SELECT id,title,markdown_uri FROM wiki_pages WHERE page_type='source'").fetchone()
    assert page["title"] == "회의록 2026-11"
    assert "\n# 회의록 2026-11\n" in read_text_object(page["markdown_uri"])


def test_finalized_draft_keeps_web_ingest_fenced_until_expiry():
    grant = str(uuid4())
    intake = _intake()
    started = _complete_intake(intake, grant)
    finalized = intake.finalize(started["run_id"], _finalize_payload(grant, started))
    assert finalized["state"] == "AWAITING_WEB_APPROVAL"
    web_run_id = str(uuid4())
    with pytest.raises(ActiveIngestRunError):
        wiki_repository.create_pipeline_run(web_run_id, "doc_1", "user-1", "workspace-1", "test", "test", "api")
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM pipeline_runs").fetchone()["count"] == 0
        conn.execute("UPDATE mcp_wiki_ingest_runs SET expires_at=now()-interval '1 second' WHERE run_id=%s", (started["run_id"],))
    assert expire_inactive_runs() == 1
    wiki_repository.create_pipeline_run(web_run_id, "doc_1", "user-1", "workspace-1", "test", "test", "api")


@pytest.mark.parametrize("link_drift", [False, True])
def test_lint_with_source_link_preserves_link_and_rechecks_approval(monkeypatch, link_drift):
    ledger, target, _ = _seed_lint_page("alpha-page", "alpha")
    monkeypatch.setattr(calculation, "read_contributions", lambda *_: [ledger])
    with _connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,user_id,workspace_id,status,created_at,updated_at) "
            "VALUES ('source-page','source','Source','source','user-1','workspace-1','active',now(),now())"
        )
        conn.execute(
            "INSERT INTO wiki_page_links(from_page_id,to_page_id,link_type,workspace_id,created_at,updated_at) "
            "VALUES ('source-page','alpha-page','source_mentions_concept','workspace-1',now(),now())"
        )
        # The publication writer derives source links from document provenance.
        conn.execute(
            "INSERT INTO document_wiki_links(document_id,wiki_page_id,relation_type,workspace_id,created_at) "
            "VALUES ('doc_seed','source-page','source_of','workspace-1',now()), "
            "('doc_seed','alpha-page','extracted_concept','workspace-1',now())"
        )
    source = Source([("B0001", "Additional alpha evidence.")])
    grant = str(uuid4())
    payload, _ = _lint_payload(grant, [target], lambda snapshot: [{
        "operation_id": "add-alpha", "kind": "UPSERT_STATEMENT",
        "target_page_id": "alpha-page", "depends_on": [],
        "arguments": {"statement_id": None, "text": "Additional alpha evidence.", "evidence": [{
            "snapshot_id": snapshot, "block_id": "B0001", "quote": "Additional alpha evidence.",
        }]},
    }], source)
    preview = _intake(source).lint(payload)
    with _connect() as conn:
        draft = dict(conn.execute("SELECT * FROM mcp_wiki_drafts").fetchone())
        assert not any(change["kind"] == "LINK" for change in draft["preview"]["changes"])
        assert apply_repository_module._recalculate_lint(conn, draft)["preview"] == draft["preview"]
        if link_drift:
            conn.execute("UPDATE wiki_page_links SET label='changed' WHERE from_page_id='source-page'")
    approval = _apply_payload(grant, preview)
    result = McpWikiDraftApply(PostgresMcpWikiApplyRepository(), _ValidApprovalAuthority()).apply(
        approval.draft_id, approval)
    assert result["state"] == ("CONFLICTED" if link_drift else "PUBLISHED")
    with _connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM wiki_page_links WHERE from_page_id='source-page' "
            "AND to_page_id='alpha-page' AND link_type='source_mentions_concept'"
        ).fetchone()["n"] == 1
        if link_drift:
            assert conn.execute("SELECT count(*) AS n FROM wiki_publications").fetchone()["n"] == 0
        else:
            page = conn.execute("SELECT markdown_uri FROM wiki_pages WHERE id='alpha-page'").fetchone()
            assert "Additional alpha evidence." in read_text_object(page["markdown_uri"])
