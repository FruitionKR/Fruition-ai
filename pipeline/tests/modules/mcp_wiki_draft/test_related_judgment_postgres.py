from uuid import uuid4

import pytest
import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401
from test_related_judgment import judge
from app.modules.model_usage.infrastructure import usage_ledger


@pytest.mark.parametrize("drift", [None, "version", "hash", "identity"])
def test_related_finalize_frozen_apply_and_target_drift(monkeypatch, drift):
    _, target, _ = h._seed_lint_page("target", "target")
    monkeypatch.setattr(h.calculation, "read_contributions", lambda *_: [])
    model = judge()
    monkeypatch.setattr(h.PostgresMcpWikiDraftRepository, "_concept_judge", staticmethod(lambda: model))
    grant = str(uuid4())
    intake = h._intake()
    started = intake.start(h._start_payload(grant))
    for ordinal, text in [(1, "Alpha concept has evidence."), (2, "Beta concept has support.")]:
        window = intake.read_window(started["run_id"], h.McpWikiActorRequest.model_validate(h._actor("get_window", grant)), ordinal)
        intake.submit(started["run_id"], window["window_id"], h._submission(
            grant, started, window, key=f"submit-{ordinal}", concept="Incoming", text=text, quote=text,
            block_id=f"B000{ordinal}"))
    allowed = h._finalize_payload(grant, started).model_copy(update={"jev_allowed": True})
    finalized = intake.finalize(started["run_id"], allowed)
    assert intake.finalize(started["run_id"], allowed) == finalized
    payload = h._apply_payload(grant, finalized, operation_id=f"related-{uuid4()}")
    with h._connect() as conn:
        draft = h.apply_repository_module._load_draft(conn, payload, lock=False)
        assert target in draft["target_pages"]
        frozen = draft["normalized_submissions"]
        assert [w["contributions"][0]["links"][0]["source_statement_id"] for w in frozen] == ["statement-1", "statement-1"]
        recalculated, _ = h.apply_repository_module._recalculate(conn, draft)
        assert recalculated["preview"] == draft["preview"]
        related = [link for link in recalculated["publication_plan"]["wiki_links"] if link["link_type"] == "concept_related_to"]
        assert len(related) == 1 and related[0]["to_page_id"] == "target"
        conn.execute("INSERT INTO document_derived_state(document_id,workspace_id,last_edit_revision,last_edit_hash,last_edited_at) "
                     "VALUES ('doc_1','workspace-1',7,%s,now())",
                     (started["source_snapshot"]["source_content_hash"].removeprefix("sha256:"),))
        if drift == "version":
            conn.execute("UPDATE wiki_pages SET updated_at=updated_at + interval '1 second' WHERE id='target'")
        elif drift == "identity":
            conn.execute("UPDATE wiki_pages SET status='deleted' WHERE id='target'")
        elif drift == "hash":
            uri = conn.execute("SELECT markdown_uri FROM wiki_pages WHERE id='target'").fetchone()["markdown_uri"]
            h.write_text_object(uri, "Changed target body")
    def forbidden():
        pytest.fail("apply must not construct Jev")
    monkeypatch.setattr(h.PostgresMcpWikiDraftRepository, "_concept_judge", staticmethod(forbidden))
    apply = h.McpWikiDraftApply(h.PostgresMcpWikiApplyRepository(), h._ValidApprovalAuthority())
    result = apply.apply(payload.draft_id, payload)
    assert result["state"] == ("CONFLICTED" if drift else "PUBLISHED")
    assert apply.apply(payload.draft_id, payload) == result
    assert len(model.calls) == 1
    with h._connect() as conn:
        links = conn.execute("SELECT * FROM wiki_page_links WHERE link_type='concept_related_to'").fetchall()
        assert len(links) == (0 if drift else 1)
        if not drift:
            assert links[0]["to_page_id"] == "target"
            assert conn.execute("SELECT count(*) AS n FROM mcp_wiki_publication_outbox").fetchone()["n"] == 1


def _ready_run(grant):
    intake = h._intake()
    started = intake.start(h._start_payload(grant))
    for ordinal, text in [(1, "Alpha concept has evidence."), (2, "Beta concept has support.")]:
        window = intake.read_window(started["run_id"], h.McpWikiActorRequest.model_validate(h._actor("get_window", grant)), ordinal)
        intake.submit(started["run_id"], window["window_id"], h._submission(
            grant, started, window, key=f"submit-{ordinal}", concept="Incoming", text=text, quote=text,
            block_id=f"B000{ordinal}"))
    return intake, started


def test_finalize_without_jev_allowed_uses_client_judgment_and_flag_retry_replays(monkeypatch):
    h._seed_lint_page("target", "target")
    monkeypatch.setattr(h.calculation, "read_contributions", lambda *_: [])
    def forbidden():
        pytest.fail("jev_allowed=false must not construct Jev")
    monkeypatch.setattr(h.PostgresMcpWikiDraftRepository, "_concept_judge", staticmethod(forbidden))
    grant = str(uuid4())
    intake, started = _ready_run(grant)

    finalized = intake.finalize(started["run_id"], h._finalize_payload(grant, started))

    with h._connect() as conn:
        frozen = conn.execute("SELECT normalized_submissions FROM mcp_wiki_drafts WHERE run_id=%s",
                              (started["run_id"],)).fetchone()["normalized_submissions"]
    assert not any("server_merge_target_page_id" in item for window in frozen for item in window["contributions"])
    retry = h._finalize_payload(grant, started).model_copy(update={"jev_allowed": True})
    assert intake.finalize(started["run_id"], retry) == finalized


def test_finalize_jev_calls_are_attributed_to_run_and_actor(monkeypatch):
    h._seed_lint_page("target", "target")
    monkeypatch.setattr(h.calculation, "read_contributions", lambda *_: [])
    model = judge()
    scopes = []
    original = model.judge_concepts
    def scoped(concepts, pool):
        scopes.append(usage_ledger._actor.get())
        return original(concepts, pool)
    monkeypatch.setattr(model, "judge_concepts", scoped)
    monkeypatch.setattr(h.PostgresMcpWikiDraftRepository, "_concept_judge", staticmethod(lambda: model))
    grant = str(uuid4())
    intake, started = _ready_run(grant)

    intake.finalize(started["run_id"], h._finalize_payload(grant, started).model_copy(update={"jev_allowed": True}))

    assert scopes == [{
        "run_id": started["run_id"], "workspace_id": "workspace-1", "user_id": "user-1",
        "kind": "mcp_wiki_ingest_jev_concept",
    }]
