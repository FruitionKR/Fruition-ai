"""MCP get_wiki_page의 lint 준비 정보(문장·링크·대상 묶음)는 lint 검증이 그대로 받는다."""
from hashlib import sha256
from uuid import uuid4

import pytest

import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401
from app.modules.mcp_wiki_draft.application import McpWikiDraftError
from app.modules.mcp_wiki_draft.lint_context import concept_lint_context


# 웹 편입 링크는 비교 키(source, target, relation) 밖의 값을 더 싣는다.
WEB_LINK = {
    "source": "concept:alpha", "target": "concept:beta", "relation": "concept_related_to",
    "confidence": 0.82, "decided_by": "jev", "entry_id": None,
}


@pytest.fixture
def pages(monkeypatch):
    alpha, _alpha_target, alpha_statement = h._seed_lint_page("alpha-page", "alpha", links=[WEB_LINK])
    beta, _beta_target, _ = h._seed_lint_page("beta-page", "beta")
    gamma, _gamma_target, _ = h._seed_lint_page("gamma-page", "gamma")
    lonely, _lonely_target, _ = h._seed_lint_page("lonely-page", "lonely")
    ledgers = [alpha, beta, gamma, lonely]
    monkeypatch.setattr(
        h.calculation, "read_contributions",
        lambda page_ids, _workspace_id: [item for item in ledgers if item["page_id"] in page_ids],
    )
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO wiki_page_links(from_page_id,to_page_id,link_type,label,confidence,workspace_id,created_at,updated_at) "
            "VALUES ('alpha-page','beta-page','concept_related_to',NULL,0.82,'workspace-1',now(),now())"
        )
        # gamma가 beta의 하위 항목을 참조한다. beta를 lint하면 gamma도 대상이어야 한다.
        conn.execute(
            "INSERT INTO wiki_page_entry_references(page_id,entry_id,target_slug,name,parent_entry_id,workspace_id) "
            "VALUES ('gamma-page','ref-entry','beta','Beta entry',NULL,'workspace-1')"
        )
    return {"alpha_statement": alpha_statement}


def _context(page_id: str) -> dict:
    with h._connect() as conn:
        row = dict(conn.execute("SELECT * FROM wiki_pages WHERE id=%s", (page_id,)).fetchone())
        return concept_lint_context(conn, "workspace-1", row)


def _targets(page_ids: list[str]) -> list[dict]:
    with h._connect() as conn:
        rows = conn.execute("SELECT * FROM wiki_pages WHERE id=ANY(%s) ORDER BY id", (page_ids,)).fetchall()
    return [
        {key: snapshot[key] for key in ("page_id", "version", "content_hash")}
        for snapshot in (h.calculation._page_snapshot(dict(row)) for row in rows)
    ]


def _lint(targets: list[dict], operations: list[dict], *, key: str):
    source = h.Source([("B0007", "Controlled lint context.")])
    payload, _snapshot = h._lint_payload(str(uuid4()), targets, operations, source, key=key)
    return h._intake(source).lint(payload)


def test_statements_carry_the_identity_and_hash_remove_statement_accepts(pages):
    context = _context("alpha-page")
    assert context["statements"] == [{
        "statement_id": pages["alpha_statement"], "text": "alpha evidence",
        "text_hash": "sha256:" + sha256(b"alpha evidence").hexdigest(),
    }]
    assert context["lint_context_error"] is None
    statement = context["statements"][0]
    result = _lint(_targets(context["lint_target_page_ids"]), [{
        "operation_id": "remove-alpha", "kind": "REMOVE_STATEMENT", "target_page_id": "alpha-page",
        "depends_on": [], "arguments": {
            "statement_id": statement["statement_id"], "expected_text_hash": statement["text_hash"],
        },
    }], key="remove-statement")
    assert result["state"] == "AWAITING_WEB_APPROVAL"


def test_links_and_target_closure_match_what_the_validator_requires(pages):
    alpha, beta, lonely = _context("alpha-page"), _context("beta-page"), _context("lonely-page")
    assert alpha["links"] == [{
        "page_id": "beta-page", "slug": "beta", "relation": "concept_related_to", "direction": "outgoing",
        "source_ref": "concept:alpha", "target_ref": "concept:beta",
    }]
    assert beta["links"] == [{
        "page_id": "alpha-page", "slug": "alpha", "relation": "concept_related_to", "direction": "incoming",
        "source_ref": "concept:alpha", "target_ref": "concept:beta",
    }]
    # alpha→beta 링크, beta를 참조하는 gamma까지 닫는다. 연결 없는 페이지는 자기 자신뿐이다.
    assert alpha["lint_target_page_ids"] == beta["lint_target_page_ids"] == ["alpha-page", "beta-page", "gamma-page"]
    assert alpha["lint_target_truncated"] is False
    assert lonely["lint_target_page_ids"] == ["lonely-page"] and lonely["links"] == []

    link = alpha["links"][0]
    remove = [{
        "operation_id": "remove-link", "kind": "REMOVE_LINK", "target_page_id": "alpha-page", "depends_on": [],
        "arguments": {"source_ref": link["source_ref"], "target_ref": link["target_ref"], "relation": link["relation"]},
    }]
    # 대상 묶음에서 하나라도 빼면 검증이 막는다.
    with pytest.raises(McpWikiDraftError):
        _lint(_targets(["alpha-page", "beta-page"]), remove, key="missing-gamma")
    # 웹 편입 링크(추가 키 포함)도 키로 비교해 지운다.
    result = _lint(_targets(alpha["lint_target_page_ids"]), remove, key="remove-link")
    with h._connect() as conn:
        changes = conn.execute(
            "SELECT changes FROM mcp_wiki_drafts WHERE draft_id=%s", (result["draft_id"],),
        ).fetchone()["changes"]
    assert [(item["kind"], item["action"]) for item in changes if item["kind"] == "LINK"] == [("LINK", "REMOVE")]


def test_add_link_of_an_existing_key_is_rejected_instead_of_duplicated(pages):
    alpha = _context("alpha-page")
    with pytest.raises(McpWikiDraftError) as error:
        _lint(_targets(alpha["lint_target_page_ids"]), [{
            "operation_id": "add-again", "kind": "ADD_LINK", "target_page_id": "alpha-page", "depends_on": [],
            "arguments": {"source_ref": "concept:alpha", "target_ref": "concept:beta", "relation": "concept_related_to"},
        }], key="add-again")
    assert error.value.code == "INVALID_LINT_OPERATION"


def test_closure_over_the_configured_target_bound_is_marked_truncated(pages, monkeypatch):
    monkeypatch.setenv("MCP_WIKI_DRAFT_LINT_MAX_TARGET_PAGES", "2")
    context = _context("alpha-page")
    assert context["lint_target_truncated"] is True
    assert len(context["lint_target_page_ids"]) > 2


def test_non_concept_pages_have_empty_lint_context(pages):
    page = {"id": "source-page", "page_type": "source", "status": "active"}
    with h._connect() as conn:
        assert concept_lint_context(conn, "workspace-1", page) == {
            "statements": [], "links": [], "lint_target_page_ids": [],
            "lint_target_truncated": False, "lint_context_error": None,
        }


def test_unavailable_ledger_is_reported_without_failing_the_page_read(pages, monkeypatch):
    monkeypatch.setattr(h.calculation, "read_contributions", lambda *_args: [])
    context = _context("alpha-page")
    assert context["statements"] == [] and context["lint_context_error"] == "STATEMENTS_UNAVAILABLE"
    assert context["links"]


def test_lint_evidence_accepts_registry_block_ids(pages):
    alpha = _context("alpha-page")
    source = h.Source([("B0007", "Alpha replacement evidence.")])
    payload, snapshot = h._lint_payload(str(uuid4()), _targets(alpha["lint_target_page_ids"]), lambda snapshot: [{
        "operation_id": "upsert-alpha", "kind": "UPSERT_STATEMENT", "target_page_id": "alpha-page", "depends_on": [],
        "arguments": {
            "statement_id": None, "text": "Alpha replacement evidence.",
            "evidence": [{"snapshot_id": snapshot, "block_id": "B0007", "quote": "Alpha replacement evidence."}],
        },
    }], source, key="registry-id")
    result = h._intake(source).lint(payload)
    with h._connect() as conn:
        changes = conn.execute(
            "SELECT changes FROM mcp_wiki_drafts WHERE draft_id=%s", (result["draft_id"],),
        ).fetchone()["changes"]
    statement = next(item for item in changes if item["kind"] == "CONCEPT_STATEMENT")
    assert statement["evidence"][0]["block_id"] == "B0007"


def test_statements_report_ledger_pending_while_the_last_publication_awaits_its_sequence(pages):
    # 직전 게시가 남긴 대기 기여는 document가 원장에 올리기 전까지 순번이 없다. 다시 읽으면 풀리는 상태다.
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pending_contributions(page_id,operation_id,workspace_id,document_id,object_key,active,publication_id) "
            "VALUES ('alpha-page','just-published','workspace-1',NULL,'wiki/workspace-1/pages/alpha-page/ops/just-published.json',true,'legacy')"
        )

    context = _context("alpha-page")

    assert context["statements"] == []
    assert context["lint_context_error"] == "LEDGER_PENDING"


def test_statements_report_unavailable_for_other_ledger_failures(pages, monkeypatch):
    monkeypatch.setattr(h.calculation, "read_contributions", lambda *_args: [])

    context = _context("alpha-page")

    assert context["lint_context_error"] == "STATEMENTS_UNAVAILABLE"
