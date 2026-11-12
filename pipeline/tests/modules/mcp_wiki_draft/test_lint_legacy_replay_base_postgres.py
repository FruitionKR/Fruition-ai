"""이전 재생 렌더러로 게시한 lint 본문 위에서도 MCP lint 미리보기·적용이 된다.

운영의 첫 MCP lint는 개념 페이지를 기여 재생으로 다시 그려 게시했다. 그때 재생은 기존 페이지를 하위 항목 집으로 쓴 편입 기여
때문에 정의 근거 표시를 두 번 붙이고 mention_count·importance_score를 0으로 덮었으며, Related Concepts를 비워 그렸다.
고친 렌더러는 그 본문을 다시 만들지 못하므로, 검증은 원장이 가리키는 저장 본문을 기준으로 쓴다.
"""
import json
from hashlib import sha256
from uuid import uuid4

import pytest

import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401
from app.modules.mcp_wiki_draft.application import McpWikiDraftError
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import rebuild_concept_page
from app.modules.wiki_ingestion.infrastructure.lint_operation_artifacts import lint_contribution
from app.modules.wiki_ingestion.infrastructure.wiki_publication import publication_id


LINK = {"source": "concept:alpha", "target": "concept:beta", "relation": "concept_related_to", "confidence": 0.9}
DEFINITION = "alpha definition"


def _write_contribution(page_id: str, operation_id: str, value: dict) -> str:
    key = f"wiki/workspace-1/pages/{page_id}/ops/{operation_id}.json"
    h.write_text_object(key, json.dumps({**value, "operation_id": operation_id, "page_id": page_id}))
    return key


def _ledger_row(page_id: str, operation_id: str, document_id: str, sequence: int) -> dict:
    return {
        "page_id": page_id, "ingest_operation_id": operation_id, "source_document_id": document_id,
        "sequence_revision": sequence, "active": True,
        "object_key": f"wiki/workspace-1/pages/{page_id}/ops/{operation_id}.json", "lint_operation_id": None,
    }


def _old_renderer_body(current: str) -> str:
    """고치기 전 재생 결과: 정의 근거 중복, 집계값 0, 빈 Related Concepts."""
    old = (
        current.replace(f"{DEFINITION} [doc_seed:B1]\n", f"{DEFINITION} [doc_seed:B1] [doc_seed:B1]\n")
        .replace("mention_count: 3", "mention_count: 0")
        .replace("importance_score: 15.75", "importance_score: 0.0")
        .replace("## Related Concepts\n- [[beta|Beta]]\n", "## Related Concepts\n- 관련 개념 없음\n")
    )
    assert old.count("mention_count: 0") == 2 and "관련 개념 없음" in old and "[doc_seed:B1] [doc_seed:B1]" in old
    return old


def _seed_alpha(*, record_hash: str | None) -> tuple[list[dict], dict, str]:
    """seed 편입 → alpha를 하위 항목 집으로 쓴 편입(오염된 정의·집계 0) → 이전 렌더러로 게시한 lint."""
    beta_ledger, beta_target, _ = h._seed_lint_page("beta-page", "beta")
    suffix = uuid4().hex[:8]
    seed, home, first_lint = f"seed-{suffix}", f"home-{suffix}", f"lint-{suffix}"
    seed_value = {
        "schema_version": 1, "document_id": "doc_seed",
        "concept": {
            "slug": "alpha", "title": "Alpha", "definition": DEFINITION,
            "display_reference_ids": ["doc_seed:B1"], "anchor_reference_ids": ["doc_seed:B1"],
            "mention_reference_ids": ["doc_seed:B1", "doc_seed:B2", "doc_seed:B3"],
            "mention_count": 3, "importance_score": 15.75,
            "evidence_claim_ids": [f"{seed}:evidence"], "source_document_ids": ["doc_seed"],
        },
        "evidence_units": [{
            "evidence_id": f"{seed}:evidence", "claim": "alpha evidence",
            "anchor_reference_ids": ["doc_seed:B1"], "source_document_id": "doc_seed",
        }],
        "source_key_points": [{"text": "alpha evidence", "anchor_reference_ids": ["doc_seed:B1"]}],
        "links": [LINK], "source_blocks": [],
    }
    # 고치기 전 _existing_concept_entry가 남긴 집 개념: 본문에서 읽은 정의(근거 표시 포함)와 0 집계값.
    home_value = {
        "schema_version": 1, "document_id": "doc_home",
        "concept": {
            "slug": "alpha", "title": "Alpha", "definition": f"{DEFINITION} [doc_seed:B1]",
            "why_page_worthy": "", "aliases": [], "tags": [],
            "anchor_reference_ids": ["doc_home:B4"], "mention_reference_ids": [], "display_reference_ids": [],
            "source_document_ids": ["doc_home"], "evidence_claim_ids": [f"{home}:evidence"],
            "mention_count": 0, "importance_score": 0.0,
        },
        "evidence_units": [{
            "evidence_id": f"{home}:evidence", "claim": "home detail evidence",
            "anchor_reference_ids": ["doc_home:B4"], "source_document_id": "doc_home",
        }],
        "source_key_points": [], "links": [], "source_blocks": [],
    }
    lint_value = lint_contribution(first_lint, {
        "page_id": "alpha-page", "slug": "alpha", "title": "Alpha", "content_action": "append_evidence",
        "claims": [{"id": "claim-1", "claim": "first lint evidence", "refs": ["doc_lint0:B1"]}],
    })
    for operation_id, value in ((seed, seed_value), (home, home_value), (first_lint, lint_value)):
        _write_contribution("alpha-page", operation_id, value)
    loaded = [
        PageContribution("alpha-page", "concept", operation_id, index, "", json.loads(h.read_text_object(
            f"wiki/workspace-1/pages/alpha-page/ops/{operation_id}.json"
        )))
        for index, operation_id in enumerate((seed, home, first_lint), start=1)
    ]
    current = rebuild_concept_page(loaded, concept_titles=lambda slugs: {slug: slug.title() for slug in slugs}).markdown
    published = _old_renderer_body(current)
    key = f"wiki/workspace-1/pages/alpha-page/ops/{first_lint}.md"
    uri = h.write_text_object(key, published)
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,summary,markdown_uri,user_id,workspace_id,status,created_at,updated_at) "
            "VALUES ('alpha-page','concept','Alpha','alpha',%s,%s,'user-1','workspace-1','active',now(),now())",
            (DEFINITION, uri),
        )
        conn.execute(
            "INSERT INTO wiki_page_links(from_page_id,to_page_id,link_type,label,confidence,workspace_id,created_at,updated_at) "
            "VALUES ('alpha-page','beta-page','concept_related_to',NULL,0.9,'workspace-1',now(),now())"
        )
        if record_hash is not None:
            conn.execute(
                "INSERT INTO wiki_publications(publication_id,operation_id,workspace_id,user_id,kind,request_hash,state) "
                "VALUES (%s,%s,'workspace-1','user-1','LINT',%s,'COMMITTED')",
                (publication_id(first_lint), first_lint, "0" * 64),
            )
            conn.execute(
                "INSERT INTO wiki_publication_objects(publication_id,page_id,object_key,content_hash,content_type) "
                "VALUES (%s,'alpha-page',%s,%s,'text/markdown; charset=utf-8')",
                (publication_id(first_lint), key, record_hash),
            )
        snapshot = h.calculation._page_snapshot(dict(conn.execute(
            "SELECT * FROM wiki_pages WHERE id='alpha-page'",
        ).fetchone()))
    ledger = [
        _ledger_row("alpha-page", seed, "doc_seed", 1),
        _ledger_row("alpha-page", home, "doc_home", 2),
        _ledger_row("alpha-page", first_lint, f"lint:{first_lint}", 3),
        beta_ledger,
    ]
    targets = [{key: snapshot[key] for key in ("page_id", "version", "content_hash")}, beta_target]
    return ledger, targets, published


def _lint(targets, key="legacy-base"):
    source = h.Source([("B0007", "Second lint evidence.")])
    grant = str(uuid4())
    payload, _ = h._lint_payload(grant, targets, lambda snapshot_id: [{
        "operation_id": "second", "kind": "UPSERT_STATEMENT", "target_page_id": "alpha-page", "depends_on": [],
        "arguments": {
            "statement_id": None, "text": "Second lint evidence.",
            "evidence": [{"snapshot_id": snapshot_id, "block_id": "B0007", "quote": "Second lint evidence."}],
        },
    }], source, key=key)
    return grant, h._intake(source).lint(payload)


@pytest.mark.parametrize("recorded", [False, True], ids=["legacy_object", "hash_recorded"])
def test_lint_accepts_base_rendered_by_the_old_replay_and_publishes_the_canonical_form(monkeypatch, recorded):
    ledger, targets, published = _seed_alpha(record_hash=None)
    if recorded:
        with h._connect() as conn:
            first_lint = ledger[2]["ingest_operation_id"]
            conn.execute(
                "INSERT INTO wiki_publications(publication_id,operation_id,workspace_id,user_id,kind,request_hash,state) "
                "VALUES (%s,%s,'workspace-1','user-1','LINT',%s,'COMMITTED')",
                (publication_id(first_lint), first_lint, "0" * 64),
            )
            conn.execute(
                "INSERT INTO wiki_publication_objects(publication_id,page_id,object_key,content_hash,content_type) "
                "VALUES (%s,'alpha-page',%s,%s,'text/markdown; charset=utf-8')",
                (publication_id(first_lint), f"wiki/workspace-1/pages/alpha-page/ops/{first_lint}.md",
                 "sha256:" + sha256(published.encode("utf-8")).hexdigest()),
            )
    monkeypatch.setattr(h.calculation, "read_contributions", lambda page_ids, _ws: [
        item for item in ledger if item["page_id"] in page_ids
    ])

    grant, preview = _lint(targets)

    with h._connect() as conn:
        draft = dict(conn.execute("SELECT * FROM mcp_wiki_drafts").fetchone())
        change = next(item for item in draft["changes"] if item["kind"] == "PAGE_BODY" and item["page_id"] == "alpha-page")
    assert change["before_text"] == published
    after = change["after_text"]
    assert f"## Definition\n{DEFINITION} [doc_seed:B1]\n" in after
    assert "[doc_seed:B1] [doc_seed:B1]" not in after
    assert "mention_count: 3" in after and "mention_count: 0" not in after
    assert "importance_score: 15.75" in after
    assert "## Related Concepts\n- [[beta|Beta]]\n" in after
    assert "Second lint evidence." in after and "first lint evidence" in after

    result = h.McpWikiDraftApply(h.PostgresMcpWikiApplyRepository(), h._ValidApprovalAuthority()).apply(
        preview["draft_id"], h._apply_payload(grant, preview),
    )
    assert result["state"] == "PUBLISHED"
    with h._connect() as conn:
        assert h.calculation._page_snapshot(dict(conn.execute(
            "SELECT * FROM wiki_pages WHERE id='alpha-page'",
        ).fetchone()))["body"] == after


def test_lint_rejects_base_object_that_does_not_match_its_publication_record(monkeypatch):
    ledger, targets, _published = _seed_alpha(record_hash="sha256:" + "f" * 64)
    monkeypatch.setattr(h.calculation, "read_contributions", lambda page_ids, _ws: [
        item for item in ledger if item["page_id"] in page_ids
    ])

    with pytest.raises(McpWikiDraftError) as rejected:
        _lint(targets, key="tampered-base")

    assert rejected.value.code == "LEDGER_CONTRADICTION"
    assert rejected.value.retryable is False


def test_lint_preview_is_retryable_while_the_last_publication_awaits_its_ledger_sequence(monkeypatch):
    ledger, targets, _published = _seed_alpha(record_hash=None)
    monkeypatch.setattr(h.calculation, "read_contributions", lambda page_ids, _ws: [
        item for item in ledger if item["page_id"] in page_ids
    ])
    # 직전 lint 게시가 남긴 대기 기여. document가 원장에 올리기 전이라 순번이 없다.
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pending_contributions(page_id,operation_id,workspace_id,document_id,object_key,active,publication_id) "
            "VALUES ('alpha-page','just-published','workspace-1',NULL,'wiki/workspace-1/pages/alpha-page/ops/just-published.json',true,'legacy')"
        )

    with pytest.raises(McpWikiDraftError) as pending:
        _lint(targets, key="pending-sequence")

    assert pending.value.code == "LEDGER_RECONCILIATION_PENDING"
    assert pending.value.retryable is True


def test_lint_contradiction_is_not_reported_as_retryable_pending(monkeypatch):
    ledger, targets, _published = _seed_alpha(record_hash=None)
    monkeypatch.setattr(h.calculation, "read_contributions", lambda page_ids, _ws: [
        item for item in ledger if item["page_id"] in page_ids
    ])
    # 공개 본문이 원장이 가리키는 마지막 게시 본문과 다르다(원장 밖에서 바뀌었다).
    with h._connect() as conn:
        conn.execute(
            "UPDATE wiki_pages SET markdown_uri=%s WHERE id='alpha-page'",
            (h.write_text_object("baseline/alpha-untracked.md", "# Alpha\n\nUntracked body.\n"),),
        )
        snapshot = h.calculation._page_snapshot(dict(conn.execute(
            "SELECT * FROM wiki_pages WHERE id='alpha-page'",
        ).fetchone()))
    targets = [{key: snapshot[key] for key in ("page_id", "version", "content_hash")}, targets[1]]

    with pytest.raises(McpWikiDraftError) as contradiction:
        _lint(targets, key="contradiction")

    assert contradiction.value.code == "LEDGER_CONTRADICTION"
    assert contradiction.value.retryable is False
