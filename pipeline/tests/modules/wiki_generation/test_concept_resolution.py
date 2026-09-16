from app.modules.wiki_generation.infrastructure.concept_resolution import (
    merge_concept_ledger,
)


def test_merges_incoming_concepts_by_canonical_slug() -> None:
    ledger = [
        {
            "slug": "back-emf",
            "title": "Back EMF",
            "aliases": [],
            "anchor_reference_ids": ["B0001"],
            "mention_reference_ids": [],
            "display_reference_ids": [],
            "source_document_ids": ["doc-1"],
            "evidence_claim_ids": ["ev-1"],
            "importance_score": 1.0,
            "definition": "짧은 정의",
        },
        {
            "slug": "counter-emf",
            "title": "Counter EMF",
            "aliases": [],
            "anchor_reference_ids": ["B0002"],
            "mention_reference_ids": ["B0002"],
            "display_reference_ids": [],
            "source_document_ids": ["doc-1"],
            "evidence_claim_ids": ["ev-2"],
            "importance_score": 2.0,
            "definition": "더 긴 역기전력 정의",
        },
    ]
    resolution_by_slug = {
        "back-emf": {"incoming_slug": "back-emf", "canonical_slug": "back-emf"},
        "counter-emf": {"incoming_slug": "counter-emf", "canonical_slug": "back-emf"},
    }

    result = merge_concept_ledger(
        ledger,
        resolution_by_slug,
        {"back-emf": "back-emf", "counter-emf": "back-emf"},
        {},
    )

    assert len(result) == 1
    assert result[0]["slug"] == "back-emf"
    assert result[0]["anchor_reference_ids"] == ["B0001", "B0002"]
    assert result[0]["definition"] == "더 긴 역기전력 정의"
