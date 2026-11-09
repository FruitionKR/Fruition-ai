"""Active publication attempts retain admission and cancellation fences."""
from uuid import uuid4
import pytest
import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401


@pytest.mark.parametrize("state", ["APPLYING", "UNKNOWN"])
def test_unresolved_ingest_blocks_web_ingest(state):
    grant = str(uuid4())
    started = h._complete_intake(h._intake(), grant)
    with h._connect() as conn:
        conn.execute("UPDATE mcp_wiki_ingest_runs SET state=%s,expires_at=now()-interval '1 day' WHERE run_id=%s", (state, started["run_id"]))
    with pytest.raises(h.ActiveIngestRunError):
        h.wiki_repository.create_pipeline_run(str(uuid4()), "doc_1", "user-1", "workspace-1", "test", "test", "api")


@pytest.mark.parametrize("state", ["APPLYING", "UNKNOWN"])
@pytest.mark.parametrize("kind", ["INGEST", "LINT"])
def test_unresolved_draft_counts_toward_admission(monkeypatch, state, kind):
    if kind == "INGEST":
        grant = str(uuid4())
        started = h._complete_intake(h._intake(), grant)
        with h._connect() as conn:
            conn.execute("UPDATE mcp_wiki_ingest_runs SET state=%s,expires_at=now()-interval '1 day' WHERE run_id=%s", (state, started["run_id"]))
    else:
        grant, draft = h._lint_apply_draft(monkeypatch)
        with h._connect() as conn:
            conn.execute("UPDATE mcp_wiki_drafts SET state=%s,expires_at=now()-interval '1 day' WHERE draft_id=%s", (state, draft["draft_id"]))
    monkeypatch.setenv("MCP_WIKI_DRAFT_MAX_ACTIVE_PER_USER", "1")
    assert h.expire_inactive_runs() == 0
    with pytest.raises(h.McpWikiDraftError) as rejected:
        h._intake().start(h._start_payload(grant, document_id="doc_2", key="second"))
    assert rejected.value.code == "ACTIVE_LIMIT_EXCEEDED"


def test_cancel_receipt_survives_late_unknown_authority_response(monkeypatch):
    grant, draft = h._lint_apply_draft(monkeypatch)
    payload = h._apply_payload(grant, draft)

    class CancelDuringLookup:
        def lookup(self, request):
            h.postgres_task_journal.request_cancel(payload.draft_id, "workspace-1", "user-1")
            h.postgres_task_journal.rollback(payload.draft_id)
            return {"status": "UNKNOWN", "reason": "AUTHZ_UNAVAILABLE"}

    apply = h.McpWikiDraftApply(h.PostgresMcpWikiApplyRepository(), CancelDuringLookup())
    result = apply.apply(payload.draft_id, payload)
    assert result["state"] == "HALTED"
    assert result["error_code"] == "CANCELLED"
    assert apply.apply(payload.draft_id, payload) == result
    assert apply.result(payload.approval_id, payload.operation_id, "workspace-1", "user-1") == result


def test_expired_unpublished_ingest_discards_private_source_and_window_text():
    grant = str(uuid4())
    intake = h._intake()
    started = h._complete_intake(intake, grant)
    intake.finalize(started["run_id"], h._finalize_payload(grant, started))
    with h._connect() as conn:
        conn.execute("UPDATE mcp_wiki_ingest_runs SET expires_at=now()-interval '1 day'")
    assert h.expire_inactive_runs() == 1
    with h._connect() as conn:
        for table in ("mcp_wiki_source_blocks", "mcp_wiki_windows", "mcp_wiki_window_submissions", "mcp_wiki_drafts"):
            assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0
        assert conn.execute("SELECT state FROM mcp_wiki_ingest_runs").fetchone()["state"] == "EXPIRED"
        assert conn.execute("SELECT count(*) AS n FROM mcp_wiki_draft_idempotency").fetchone()["n"] > 0
