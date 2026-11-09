"""Regression: distinct accepted names resolving to one page retain every claim."""
from types import SimpleNamespace
from uuid import uuid4

from app.modules.mcp_wiki_draft import calculation


def test_title_and_official_alias_keep_both_contributions(monkeypatch):
    index = [{"id": "page1", "slug": "alpha", "title": "Alpha", "identity_aliases": ["Official alias"], "status": "active"}]
    monkeypatch.setattr(calculation, "load_concept_identity_index", lambda *args, **kwargs: index)
    monkeypatch.setattr(calculation, "read_contributions", lambda *args: [])
    monkeypatch.setattr(calculation, "with_pending_contributions", lambda *args, **kwargs: [])
    monkeypatch.setattr(calculation, "replay_concept_page_with_operation", lambda **kwargs: ([], SimpleNamespace(
        markdown="", entries=[], supported_links=[], deleted=False,
    )))

    class Connection:
        def execute(self, *args):
            return self
        def fetchall(self):
            return []
        def fetchone(self):
            return None

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
    result = calculation.calculate_ingest_preview(
        Connection(),
        run={"workspace_id": "ws", "document_id": "doc1", "draft_id": str(uuid4()),
             "snapshot_id": snapshot_id, "source_version": 1, "source_content_hash": "sha256:" + "a" * 64},
        submissions=[{"window_ordinal": 1, "contributions": concepts}],
        source_blocks=[{"block_id": "B1", "text": "First fact"}, {"block_id": "B2", "text": "Second fact"}],
    )
    contribution = result["calculation_inputs"]["contributions"]["alpha"]
    assert {item["claim"] for item in contribution["evidence_units"]} == {"First fact", "Second fact"}
    assert {item["block_id"] for item in contribution["source_blocks"]} == {"B1", "B2"}
    statements = [item for item in result["preview"]["changes"] if item["kind"] == "CONCEPT_STATEMENT"]
    assert {item["after_text"] for item in statements} == {"First fact", "Second fact"}
    assert {item["page_id"] for item in statements} == {"page1"}
    assert {item["evidence"][0]["snapshot_id"] for item in statements} == {snapshot_id}
