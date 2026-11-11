from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.modules.mcp_wiki_draft import calculation as c
from test_semantic_merge import FakeJudge, contribution


@pytest.fixture
def index(monkeypatch):
    rows = [{"id": "target", "title": "Target", "slug": "target", "status": "active",
             "summary": "Target definition", "identity_aliases": []}]
    monkeypatch.setattr(c, "load_concept_identity_index", lambda *args, **kwargs: rows)
    monkeypatch.setattr(c, "load_submission_policy", lambda: SimpleNamespace(max_links=5))
    return rows


def judge(targets=None):
    return FakeJudge({"mcp-incoming-0": {"decision": "link_to", "canonical_slug": "target",
                                        "link_targets": targets or ["target", "target"]}})


@pytest.mark.parametrize("length, expected", [(498, [1, 2]), (499, [1]), (500, [1])])
def test_exact_cutoff_provenance_across_windows(index, length, expected, monkeypatch):
    submissions = []
    for ordinal, text in enumerate(["x" * length, "second", "third", "fourth"], 1):
        item = contribution("Incoming")
        item["statements"][0]["text"] = text
        submissions.append({"window_ordinal": ordinal, "contributions": [item]})
    model = judge()
    original = model.judge_concepts
    def captured(concepts, pool):
        assert concepts[0]["definition"] == " ".join(["x" * length, "second", "third"])[:500]
        return original(concepts, pool)
    monkeypatch.setattr(model, "judge_concepts", captured)
    frozen = c.freeze_server_concept_merges(None, workspace_id="w", submissions=submissions, judge=model)
    assert [w["window_ordinal"] for w in frozen if w["contributions"][0]["links"]] == expected
    for window in frozen:
        item = window["contributions"][0]
        assert item["statements"] == submissions[window["window_ordinal"] - 1]["contributions"][0]["statements"]
        assert all(link["source_statement_id"] == "Incoming" for link in item["links"])
    # A client duplicate must not create another edge alongside the frozen decision.
    frozen[0]["contributions"][0]["links"].append({
        "source_statement_id": "Incoming", "target_concept_name": "Target", "relation": "concept_related_to",
    })
    combined = c._combined_concepts(frozen)
    assert {link["source_statement_id"] for link in combined[0]["links"]} == {f"{i}:Incoming" for i in expected}
    allocated, _ = c._allocate_identities(None, draft_id=str(uuid4()), workspace_id="w", concepts=combined)
    _, links = c._normalized({"document_id": "doc", "source_display_name": "Doc"}, combined, allocated, index)
    assert links == [{"source": "concept:incoming", "target": "concept:target", "relation": "concept_related_to"}]
    assert len(model.calls) == 1
    assert all(not w["contributions"][0]["links"] for w in submissions)


def test_three_statement_limit_and_post_allocation_self_link(index):
    submissions = [{"window_ordinal": i, "contributions": [contribution("Target")]} for i in range(1, 5)]
    frozen = c.freeze_server_concept_merges(None, workspace_id="w", submissions=submissions, judge=judge())
    assert [bool(w["contributions"][0]["links"]) for w in frozen] == [True, True, True, False]
    concepts = c._combined_concepts(frozen)
    allocated, _ = c._allocate_identities(None, draft_id=str(uuid4()), workspace_id="w", concepts=concepts)
    assert c._normalized({"document_id": "doc", "source_display_name": "Doc"}, concepts, allocated, index)[1] == []


def test_foreign_and_stale_targets_fail_closed(index):
    submissions = [{"window_ordinal": 1, "contributions": [contribution("Incoming")]}]
    with pytest.raises(c.SemanticMergeError):
        c.freeze_server_concept_merges(None, workspace_id="w", submissions=submissions, judge=judge(["foreign"]))
    frozen = c.freeze_server_concept_merges(None, workspace_id="w", submissions=submissions, judge=judge())
    concepts = c._combined_concepts(frozen)
    allocated, _ = c._allocate_identities(None, draft_id=str(uuid4()), workspace_id="w", concepts=concepts)
    index[0]["id"] = "replacement-with-same-name"
    with pytest.raises(c.SemanticMergeError):
        c._normalized({"document_id": "doc", "source_display_name": "Doc"}, concepts, allocated, index)


def test_server_links_obey_existing_window_bound(index, monkeypatch):
    monkeypatch.setattr(c, "load_submission_policy", lambda: SimpleNamespace(max_links=1))
    item = contribution("Incoming")
    item["statements"].append({**item["statements"][0], "statement_id": "second"})
    with pytest.raises(c.McpWikiDraftError, match="link bound"):
        c.freeze_server_concept_merges(None, workspace_id="w", submissions=[{"window_ordinal": 1, "contributions": [item]}], judge=judge())
