import pytest

from app.modules.mcp_wiki_draft.calculation import _removed_statement_changes
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution


def _loaded():
    return [PageContribution("page1", "concept", "old", 1, "", {
        "operation_id": "old", "document_id": "doc1",
        "concept": {"slug": "alpha"},
        "evidence_units": [
            {"evidence_id": "first", "claim": "Old first claim", "anchor_reference_ids": ["B1"]},
            {"evidence_id": "second", "claim": "Unchanged second claim", "anchor_reference_ids": ["B2"]},
        ],
    })]


@pytest.mark.parametrize("blocks, expected", [
    (["doc1:B1"], {"Old first claim"}),
    (["doc1:B1", "doc1:B2"], {"Old first claim", "Unchanged second claim"}),
    (["doc_other:B1"], set()),
])
def test_preview_removals_follow_common_replay_including_last_claim(blocks, expected):
    changes = _removed_statement_changes("page1", _loaded(), {
        "operation_id": "new", "document_id": "doc1",
        "artifact_type": "invalidation", "invalidated_reference_ids": blocks,
    })
    assert {item["before_text"] for item in changes} == expected
    assert all(item["kind"] == "CONCEPT_STATEMENT" and item["action"] == "REMOVE" for item in changes)
    assert all(item["page_id"] == "page1" and item["after_text"] is None and item["evidence"] == [] for item in changes)
    assert [item["subject_id"] for item in changes] == sorted(item["subject_id"] for item in changes)


def test_preview_does_not_remove_claims_already_invalidated_by_earlier_operations():
    loaded = _loaded()
    loaded.append(PageContribution("page1", "concept", "prior", 2, "", {
        "operation_id": "prior", "document_id": "doc1", "artifact_type": "invalidation",
        "invalidated_reference_ids": ["doc1:B1"],
    }))
    changes = _removed_statement_changes("page1", loaded, {
        "operation_id": "new", "document_id": "doc1", "artifact_type": "invalidation",
        "invalidated_reference_ids": ["doc1:B1", "doc1:B2"],
    })
    assert len(changes) == 1
    assert changes[0]["subject_id"] == "old:second"
    assert changes[0]["before_text"] == "Unchanged second claim"
