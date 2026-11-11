"""Regression: distinct accepted names resolving to one page retain every claim."""
from uuid import uuid4

from app.modules.mcp_wiki_draft import calculation


def test_title_and_official_alias_keep_both_contributions():
    index = [{"page_id": "page1", "slug": "alpha", "title": "Alpha", "identity_aliases": ["Official alias"],
              "status": "active", "definition": ""}]
    snapshot_id = str(uuid4())
    concepts = [
        {"concept_name": name, "statements": [{
            "statement_id": sid, "text": text,
            "evidence": [{"snapshot_id": snapshot_id, "block_id": block_id, "quote": text}],
        }], "links": []}
        for name, sid, text, block_id in (
            ("Alpha", "s1", "First fact", "B1"),
            ("Official alias", "s2", "Second fact", "B2"),
        )
    ]
    manifest = calculation.build_ingest_manifest(
        run={"workspace_id": "ws", "document_id": "doc1", "user_id": "user",
             "source_content_hash": "sha256:" + "a" * 64, "source_display_name": "Doc1"},
        submissions=[{"window_ordinal": 1, "contributions": concepts}],
        source_blocks=[{"block_id": "B1", "ordinal": 1, "text": "First fact"},
                       {"block_id": "B2", "ordinal": 2, "text": "Second fact"}],
        current_source_blocks=[],
        concept_index=index,
        operation_id="operation",
    )
    # 웹 이름 일치가 제목과 공식 별칭을 같은 페이지로 합친다. 두 문장 모두 그 페이지 기여에 남는다.
    assert [item["slug"] for item in manifest["normalized"]["concept_ledger"]] == ["alpha"]
    contribution = manifest["concept_contributions"]["alpha"]
    assert {item["claim"] for item in contribution["evidence_units"]} == {"First fact", "Second fact"}
    assert {item["block_id"] for item in contribution["source_blocks"]} == {"B1", "B2"}
