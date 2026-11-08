from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.mcp_wiki_draft import calculation
from app.modules.mcp_wiki_draft.interfaces.http.schemas import ContributionRequest


def contribution(name, target=None):
    value = {
        "concept_name": name,
        "statements": [{"statement_id": name, "text": "Evidence", "evidence": [{"block_id": "B1"}]}],
        "links": [],
    }
    if target is not None:
        value["merge_target_page_id"] = target
    return value


def test_semantic_target_overrides_name_match_and_preserves_canonical_title(monkeypatch):
    index = [
        {"id": "canonical", "title": "Authentication", "slug": "authentication", "status": "active"},
        {"id": "homonym", "title": "Login", "slug": "login", "status": "active"},
    ]
    def load(_conn, workspace, **_kwargs):
        assert workspace == "workspace"
        return index
    monkeypatch.setattr(calculation, "load_concept_identity_index", load)
    concepts = calculation._combined_concepts([
        {"window_ordinal": 1, "contributions": [contribution("Login", "canonical")]},
        {"window_ordinal": 2, "contributions": [contribution("Sign in", "canonical")]},
    ])
    allocated, _ = calculation._allocate_identities(
        None, draft_id=str(uuid4()), workspace_id="workspace", concepts=concepts,
    )
    assert {item["page_id"] for item in allocated.values()} == {"canonical"}
    assert {item["title"] for item in allocated.values()} == {"Authentication"}
    normalized, _ = calculation._normalized({"document_id": "doc_1"}, concepts, allocated, index)
    assert {item["title"] for item in normalized["concept_ledger"]} == {"Authentication"}
    assert {item["related_concept_slugs"][0] for item in normalized["evidence_units"]} == {"authentication"}


@pytest.mark.parametrize("index", [[], [{"id": "target", "title": "Target", "slug": "target", "status": "deleted"}]])
def test_unavailable_target_never_falls_back_to_a_new_page(monkeypatch, index):
    monkeypatch.setattr(calculation, "load_concept_identity_index", lambda *_args, **_kwargs: index)
    with pytest.raises(calculation.SemanticMergeError):
        calculation._allocate_identities(
            None, draft_id=str(uuid4()), workspace_id="workspace", concepts=[contribution("Other name", "target")],
        )


def test_cross_window_decision_is_preserved_and_conflicting_targets_are_rejected():
    submissions = [
        {"window_ordinal": 1, "contributions": [contribution("Login")]},
        {"window_ordinal": 2, "contributions": [contribution("Login", "target")]},
    ]
    combined = calculation._combined_concepts(submissions)
    assert combined[0]["merge_target_page_id"] == "target"
    assert [item["statement_id"] for item in combined[0]["statements"]] == ["1:Login", "2:Login"]
    submissions.append({"window_ordinal": 3, "contributions": [contribution("Login", "different")]})
    with pytest.raises(calculation.SemanticMergeError):
        calculation._combined_concepts(submissions)


def test_target_schema_is_bounded_and_legacy_contribution_remains_valid():
    value = {"concept_name": "Login", "statements": [], "links": []}
    assert ContributionRequest(**value).merge_target_page_id is None
    for invalid in ("", " padded", "https://example.com", "a" * 256, 123):
        with pytest.raises(ValidationError):
            ContributionRequest(**value, merge_target_page_id=invalid)
