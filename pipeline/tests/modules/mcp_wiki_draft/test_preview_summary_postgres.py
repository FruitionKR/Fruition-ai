"""승인 화면용 `preview.summary`: 실제 lint·편입 미리보기 계산과 저장·조회 경로."""
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import api
import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401
from app.modules.mcp_wiki_draft.calculation import _ingest_summary
from app.modules.mcp_wiki_draft.interfaces.http.dependencies import get_mcp_wiki_draft_intake
from app.modules.mcp_wiki_draft.preview_summary import MAX_INVALIDATION_ITEMS, plain_text
from app.modules.wiki_generation.domain.text_utils import slugify

SUMMARY_KEYS = {"kind", "page_id", "page_title", "text", "before_text", "related_page_title", "evidence"}


def _routed_preview(intake, draft_id: str) -> dict:
    api.app.dependency_overrides[get_mcp_wiki_draft_intake] = lambda: intake
    with TestClient(api.app) as client:
        response = client.get(
            f"/internal/mcp/wiki/drafts/{draft_id}/preview?draft_version=1",
            headers={
                "X-Agent-Service-Token": "agent-test-token",
                "X-MCP-Workspace-Id": "workspace-1", "X-MCP-User-Id": "user-1",
            },
        )
    assert response.status_code == 200
    return response.json()


def test_lint_remove_and_add_summarize_statements_with_source_quote(monkeypatch):
    ledger, target, statement_id = h._seed_lint_page("alpha-page", "alpha")
    monkeypatch.setattr(h.calculation, "read_contributions", lambda page_ids, workspace_id: [ledger])
    source = h.Source([("B0001", "Alpha has a new statement.")])
    payload, snapshot_id = h._lint_payload(str(uuid4()), [target], lambda snapshot: [
        {
            "operation_id": "remove-alpha", "kind": "REMOVE_STATEMENT", "target_page_id": "alpha-page",
            "depends_on": [], "arguments": {
                "statement_id": statement_id,
                "expected_text_hash": "sha256:" + sha256(b"alpha evidence").hexdigest(),
            },
        },
        {
            "operation_id": "add-alpha", "kind": "UPSERT_STATEMENT", "target_page_id": "alpha-page",
            "depends_on": [], "arguments": {
                "statement_id": None, "text": "Alpha has a new statement.",
                "evidence": [{"snapshot_id": snapshot, "block_id": "B0001", "quote": "Alpha has a new statement."}],
            },
        },
    ], source)
    intake = h._intake(source)
    result = intake.lint(payload)
    preview = _routed_preview(intake, result["draft_id"])
    assert preview == intake.preview(result["draft_id"], 1, "workspace-1", "user-1")
    assert all(set(item) == SUMMARY_KEYS for item in preview["summary"])
    assert preview["summary"] == [
        {
            "kind": "STATEMENT_REMOVE", "page_id": "alpha-page", "page_title": "Alpha",
            "text": "alpha evidence", "before_text": None, "related_page_title": None, "evidence": [],
        },
        {
            "kind": "STATEMENT_ADD", "page_id": "alpha-page", "page_title": "Alpha",
            "text": "Alpha has a new statement.", "before_text": None, "related_page_title": None,
            "evidence": [{"snapshot_id": snapshot_id, "block_id": "B0001", "quote": "Alpha has a new statement."}],
        },
    ]
    with h._connect() as conn:
        stored = conn.execute("SELECT preview FROM mcp_wiki_drafts WHERE draft_id=%s", (result["draft_id"],)).fetchone()
    assert stored["preview"]["summary"] == preview["summary"]


def test_lint_update_summary_carries_previous_statement_text(monkeypatch):
    grant, result = h._lint_apply_draft(monkeypatch)
    preview = h._intake().preview(result["draft_id"], 1, "workspace-1", "user-1")
    [item] = preview["summary"]
    assert item["kind"] == "STATEMENT_UPDATE" and item["page_id"] == "alpha-page" and item["page_title"] == "Alpha"
    assert item["before_text"] == "alpha evidence"
    assert item["text"] == "Alpha replacement evidence."
    assert [quote["quote"] for quote in item["evidence"]] == ["Alpha replacement evidence."]
    # 요약은 승인 해시 입력이 아니다. 같은 draft를 적용하면 그대로 발행된다.
    apply_payload = h._apply_payload(grant, preview)
    applied = h.McpWikiDraftApply(h.PostgresMcpWikiApplyRepository(), h._ValidApprovalAuthority()).apply(
        apply_payload.draft_id, apply_payload,
    )
    assert applied["state"] == "PUBLISHED"


def test_lint_link_summary_names_both_concepts(monkeypatch):
    alpha, alpha_target, _ = h._seed_lint_page("alpha-page", "alpha")
    beta, beta_target, _ = h._seed_lint_page("beta-page", "beta")
    monkeypatch.setattr(h.calculation, "read_contributions", lambda page_ids, workspace_id: [alpha, beta])
    source = h.Source([("B0001", "Controlled lint context.")])
    payload, _snapshot = h._lint_payload(str(uuid4()), [alpha_target, beta_target], [{
        "operation_id": "add-related", "kind": "ADD_LINK", "target_page_id": "alpha-page", "depends_on": [],
        "arguments": {"source_ref": "concept:alpha", "target_ref": "concept:beta", "relation": "concept_related_to"},
    }], source)
    intake = h._intake(source)
    result = intake.lint(payload)
    assert intake.preview(result["draft_id"], 1, "workspace-1", "user-1")["summary"] == [{
        "kind": "LINK_ADD", "page_id": "alpha-page", "page_title": "Alpha", "text": None,
        "before_text": None, "related_page_title": "Beta", "evidence": [],
    }]


def test_ingest_summary_lists_source_new_concepts_and_quoted_statements():
    grant = str(uuid4())
    intake = h._intake()
    started = h._complete_intake(intake, grant)
    intake.finalize(started["run_id"], h._finalize_payload(grant, started))
    preview = _routed_preview(intake, started["draft_id"])
    snapshot = started["source_snapshot"]["snapshot_id"]
    assert all(set(item) == SUMMARY_KEYS for item in preview["summary"])
    assert [(item["kind"], item["page_id"], item["page_title"], item["text"]) for item in preview["summary"]] == [
        ("SOURCE_PAGE", None, "Source document", None),
        ("CONCEPT_CREATE", None, "Alpha", None),
        ("STATEMENT_ADD", None, "Alpha", "Alpha concept has evidence."),
        ("CONCEPT_CREATE", None, "Beta", None),
        ("STATEMENT_ADD", None, "Beta", "Beta concept has support."),
    ]
    assert preview["summary"][2]["evidence"] == [
        {"snapshot_id": snapshot, "block_id": "B0001", "quote": "Alpha concept has evidence."},
    ]
    assert preview["summary"][4]["evidence"] == [
        {"snapshot_id": snapshot, "block_id": "B0002", "quote": "Beta concept has support."},
    ]


def test_preview_item_bound_counts_summary(monkeypatch):
    grant = str(uuid4())
    intake = h._intake()
    started = h._complete_intake(intake, grant)
    intake.finalize(started["run_id"], h._finalize_payload(grant, started, key="finalize-a"))
    preview = intake.preview(started["draft_id"], 1, "workspace-1", "user-1")
    total = len(preview["changes"]) + len(preview["summary"])
    with h._connect() as conn:
        conn.execute("DELETE FROM mcp_wiki_drafts")
        conn.execute("UPDATE mcp_wiki_ingest_runs SET state='READY_TO_FINALIZE' WHERE run_id=%s", (started["run_id"],))
    # 변경 항목 수만으로는 상한 안이지만 요약까지 세면 넘는다. 잘라 싣지 않고 전부 거절한다.
    monkeypatch.setenv("MCP_WIKI_DRAFT_PREVIEW_MAX_ITEMS", str(total - 1))
    assert len(preview["changes"]) <= total - 1
    with pytest.raises(h.McpWikiDraftError) as rejected:
        intake.finalize(started["run_id"], h._finalize_payload(grant, started, key="finalize-b"))
    assert rejected.value.code == "PREVIEW_TOO_LARGE"
    monkeypatch.setenv("MCP_WIKI_DRAFT_PREVIEW_MAX_ITEMS", str(total))
    intake.finalize(started["run_id"], h._finalize_payload(grant, started, key="finalize-c"))


def _page(page_type: str, slug: str, title: str, status: str = "active") -> dict:
    return {"page_type": page_type, "slug": slug, "title": title, "status": status}


def test_ingest_summary_existing_pages_revival_and_capped_invalidations():
    before = {
        ("page", "src"): _page("source", slugify("doc_1"), "Doc"),
        ("page", "kept"): _page("concept", "kept", "Kept"),
        ("page", "gone"): _page("concept", "gone", "Gone", "deleted"),
    }
    after = {**before, ("page", "gone"): _page("concept", "gone", "Gone")}
    landed = [
        ({"text": "Kept fact [doc_1:B0003]", "evidence": [{"snapshot_id": "s", "block_id": "B0003", "quote": "q"}]}, "kept"),
        ({"text": "Revived fact", "evidence": []}, "gone"),
        ({"text": "Second kept fact", "evidence": []}, "kept"),
    ]
    invalidated = [("kept", f"Old claim {index}") for index in range(MAX_INVALIDATION_ITEMS + 3)]
    summary = _ingest_summary(
        document_id="doc_1", display_name="Doc", before=before, after=after,
        landed=landed, invalidated_statements=invalidated,
    )
    head = [(item["kind"], item["page_id"], item["text"]) for item in summary[:5]]
    assert head == [
        ("SOURCE_PAGE", "src", None),
        ("STATEMENT_ADD", "kept", "Kept fact"),
        ("STATEMENT_ADD", "kept", "Second kept fact"),
        ("CONCEPT_CREATE", "gone", None),
        ("STATEMENT_ADD", "gone", "Revived fact"),
    ]
    tail = summary[5:]
    assert len(tail) == MAX_INVALIDATION_ITEMS + 1
    assert all(item["kind"] == "EVIDENCE_INVALIDATION" and item["page_title"] == "Kept" for item in tail)
    assert [item["text"] for item in tail[:-1]] == [f"Old claim {index}" for index in range(MAX_INVALIDATION_ITEMS)]
    assert tail[-1]["text"] is None


def test_plain_text_drops_reference_markers():
    assert plain_text("Claim text [doc_abc:B0001] more [x-y:B12]") == "Claim text more"
