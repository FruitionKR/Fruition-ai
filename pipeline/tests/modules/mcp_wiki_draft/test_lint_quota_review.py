"""Mixed draft admission uses the same workspace and user storage budget."""
from uuid import uuid4

import pytest
import test_mcp_wiki_draft as helpers
from test_mcp_wiki_draft import isolated_state  # noqa: F401


@pytest.mark.parametrize("first", ["lint", "ingest"])
@pytest.mark.parametrize("limit", ["MAX_ACTIVE_PER_USER", "MAX_ACTIVE_PER_WORKSPACE", "MAX_ACTIVE_STORAGE_BYTES"])
def test_ingest_and_lint_share_admission_limits(monkeypatch, first, limit):
    ledger, target, statement_id = helpers._seed_lint_page("alpha-page", "alpha")
    monkeypatch.setattr(helpers.calculation, "read_contributions", lambda *args: [ledger])
    source = helpers.Source([("B0001", "Alpha replacement evidence.")])
    grant = str(uuid4())
    payload, _ = helpers._lint_payload(grant, [target], lambda snapshot: [{
        "operation_id": "replace-alpha", "kind": "UPSERT_STATEMENT",
        "target_page_id": "alpha-page", "depends_on": [],
        "arguments": {"statement_id": statement_id, "text": "Alpha replacement evidence.",
                      "evidence": [{"snapshot_id": snapshot, "block_id": "B0001",
                                    "quote": "Alpha replacement evidence."}]},
    }], source)
    budget = len(source.blocks[0][1].encode()) if limit.endswith("STORAGE_BYTES") else 1
    monkeypatch.setenv("MCP_WIKI_DRAFT_" + limit, str(budget))
    intake = helpers._intake(source)
    calls = {"lint": lambda: intake.lint(payload),
             "ingest": lambda: intake.start(helpers._start_payload(grant))}
    calls[first]()
    with pytest.raises(helpers.McpWikiDraftError) as rejected:
        calls["ingest" if first == "lint" else "lint"]()
    assert rejected.value.code == "ACTIVE_LIMIT_EXCEEDED"


def test_merge_rejects_sources_with_different_recorded_parents(monkeypatch):
    import json

    ledger, targets = [], []
    for page_id in ("parent-a", "parent-b", "child-a", "child-b"):
        record, target, _ = helpers._seed_lint_page(page_id, page_id)
        if page_id.startswith("child"):
            contribution = json.loads(helpers.read_text_object(record["object_key"]))
            contribution["split_from"] = {"page_id": page_id.replace("child", "parent"), "entry_id": "root-" + page_id}
            helpers.write_text_object(record["object_key"], json.dumps(contribution))
        ledger.append(record)
        targets.append(target)
    monkeypatch.setattr(helpers.calculation, "read_contributions", lambda *args: ledger)
    source = helpers.Source()
    payload, _ = helpers._lint_payload(str(uuid4()), targets, [{
        "operation_id": "merge", "kind": "MERGE_CONCEPTS", "target_page_id": "parent-a", "depends_on": [],
        "arguments": {"source_page_ids": ["child-a", "child-b"], "destination_page_id": "parent-a"},
    }], source)
    with pytest.raises(helpers.McpWikiDraftError) as rejected:
        helpers._intake(source).lint(payload)
    assert rejected.value.code == "INVALID_LINT_OPERATION"
