from itertools import permutations

import pytest

from app.modules.wiki_generation.domain.concept_identity import ConceptSlugExhausted
from app.modules.wiki_generation.domain.entities import SourceBlock, SourceDocument
from app.modules.wiki_generation.infrastructure.assemble import _source_extraction_artifact
from app.modules.wiki_generation.infrastructure.concept_resolution import (
    allocate_concept_slugs,
    merge_concept_ledger,
)
from app.modules.wiki_generation.infrastructure.jev_concept_resolver import (
    NameMatchConceptResolver,
    _exact_name_match,
)
from app.modules.wiki_generation.infrastructure.normalize import SemanticNormalizer
from app.modules.wiki_generation.infrastructure.post_ingest_cluster import _core_concept_promotions
from app.modules.wiki_generation.infrastructure.source_context_merge import source_page_context_normalized
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    _merge_contributions,
    _without_stale,
)


def _normalized_titles(titles: tuple[str, ...]) -> dict:
    document = SourceDocument("doc-1", "Symbols", "symbols.md", "hash")
    blocks = [SourceBlock("doc-1", "B0001", "ref-1", "C, C#, C++", 1, 1)]
    note = {
        "chunk_id": "chunk-1",
        "core_concepts": [
            {
                "title": title,
                "slug_hint": title,
                "definition": title,
                "evidence_block_ids": ["B0001"],
            }
            for title in titles
        ],
        "evidence_claims": [
            {
                "evidence_id": f"ev-{index}",
                "claim": title,
                "anchor_block_ids": ["B0001"],
                "related_concept_hints": [title],
            }
            for index, title in enumerate(titles)
        ],
    }
    return SemanticNormalizer(document, blocks).normalize_notes([note])


def test_symbol_names_allocate_deterministically_and_keep_evidence_separate() -> None:
    expected = {"C": "c", "C#": "c-2", "C++": "c-3"}
    for titles in permutations(("C", "C#", "C++")):
        normalized = _normalized_titles(titles)
        assert {item["title"]: item["slug"] for item in normalized["concept_ledger"]} == expected
        evidence_by_claim = {
            item["claim"]: item["related_concept_slugs"]
            for item in normalized["evidence_units"]
        }
        assert evidence_by_claim == {title: [slug] for title, slug in expected.items()}


def test_exact_match_uses_title_or_proven_identity_alias_never_slug_or_legacy_alias() -> None:
    assert _exact_name_match(
        {"slug": "new", "title": "Known Alias", "identity_aliases": []},
        [{"slug": "canonical", "title": "Canonical", "identity_aliases": ["Known Alias"]}],
        {"new"},
    ) == "canonical"
    assert _exact_name_match(
        {"slug": "c-2", "title": "C#", "identity_aliases": []},
        [{"slug": "c", "title": "C", "aliases": ["C#"], "identity_aliases": []}],
        {"c-2"},
    ) is None
    assert _exact_name_match(
        {"slug": "c", "title": "C#", "identity_aliases": []},
        [{"slug": "c", "title": "C", "identity_aliases": []}],
        {"c"},
    ) is None


def test_exact_match_rejects_ambiguous_identity_alias() -> None:
    candidates = [
        {"slug": "one", "title": "One", "identity_aliases": ["Shared"]},
        {"slug": "two", "title": "Two", "identity_aliases": ["Shared"]},
    ]
    assert _exact_name_match(
        {"slug": "shared", "title": "Shared", "identity_aliases": []}, candidates, {"shared"}
    ) is None


def test_legacy_aliases_cannot_be_promoted_by_a_blanket_identity_flag() -> None:
    assert _exact_name_match(
        {"slug": "c-2", "title": "C#"},
        [{"slug": "c", "title": "C", "aliases": ["C#"], "aliases_are_identity": True}],
        {"c-2"},
    ) is None


def test_same_slug_different_name_is_excluded_from_fallback_identity_candidates() -> None:
    class RecordingFallback:
        def __init__(self) -> None:
            self.existing = None

        def resolve(self, _incoming, existing, _hints=None):
            self.existing = existing
            return {"resolutions": [], "hint_resolutions": []}

    fallback = RecordingFallback()
    result = NameMatchConceptResolver(fallback).resolve(
        [{"slug": "c", "title": "C#", "identity_aliases": []}],
        [{"slug": "c", "title": "C", "identity_aliases": []}],
        [],
    )
    assert result["resolutions"] == []
    assert fallback.existing == []


def test_allocation_remaps_evidence_links_and_respects_deleted_ambiguity() -> None:
    resolved = {
        "concept_ledger": [{"slug": "c", "title": "C#", "identity_aliases": []}],
        "evidence_units": [{"related_concept_slugs": ["c"]}],
        "alias_concept_links": [{"source": "c", "target": "other"}],
        "concept_slug_map": {"c": "c"},
        "hint_slug_map": {"C#": "c"},
        "concept_resolutions": [{"canonical_slug": "c", "link_targets": ["c"]}],
    }
    result = allocate_concept_slugs(
        resolved,
        [
            {"slug": "c", "title": "C", "identity_aliases": [], "status": "active"},
            {"slug": "c-2", "title": "C#", "identity_aliases": [], "status": "deleted"},
            {"slug": "c-3", "title": "C#", "identity_aliases": [], "status": "deleted"},
        ],
    )
    assert result["concept_ledger"][0]["slug"] == "c-4"
    assert result["evidence_units"][0]["related_concept_slugs"] == ["c-4"]
    assert result["alias_concept_links"][0]["source"] == "c-4"
    assert result["concept_slug_map"] == {"c": "c-4"}
    assert result["hint_slug_map"] == {"C#": "c-4"}


def test_allocation_fails_when_common_candidate_set_is_exhausted() -> None:
    with pytest.raises(ConceptSlugExhausted, match="c"):
        allocate_concept_slugs(
            {"concept_ledger": [{"slug": "c", "title": "C#", "identity_aliases": []}]},
            [
                {"slug": slug, "title": f"Other {slug}", "status": "active", "identity_aliases": []}
                for slug in ["c", *(f"c-{number}" for number in range(2, 10))]
            ],
        )


def test_ledger_does_not_turn_original_slug_into_identity_alias() -> None:
    [concept] = merge_concept_ledger(
        [{"slug": "old-slug", "title": "Name", "aliases": [], "identity_aliases": []}],
        {"old-slug": {"decision": "merge_into"}},
        {"old-slug": "canonical"},
        {},
    )
    assert "old-slug" not in concept["aliases"]
    assert concept["identity_aliases"] == []


def test_promotion_and_source_reingest_do_not_treat_symbol_slug_collisions_as_identity() -> None:
    assert _core_concept_promotions(
        {"c": "representative: C\n"},
        [{"slug": "c-2", "title": "C#", "identity_aliases": []}],
    ) == {}
    artifact = {
        "document_id": "doc-1",
        "core_concepts": [{"term": "C", "slug": "c", "aliases": ["legacy-c-sharp"]}],
        "section_candidates": [],
        "mentions": [],
    }
    normalized = {
        "concept_ledger": [{"slug": "c", "title": "C#", "aliases": ["C#"], "identity_aliases": []}],
        "section_candidates": [],
        "mentions": [],
    }
    result = source_page_context_normalized(normalized, artifact)
    assert [(item["title"], item["slug"]) for item in result["concept_ledger"]] == [("C", "c"), ("C#", "c-2")]
    assert result["concept_ledger"][0]["aliases"] == ["legacy-c-sharp", "C"]
    assert result["concept_ledger"][0]["identity_aliases"] == []


def test_source_artifact_carries_only_explicit_identity_alias_provenance() -> None:
    artifact = _source_extraction_artifact(
        {
            "document": {"document_id": "doc-1", "source_path": "doc.md"},
            "semantic_notes": [],
            "concept_ledger": [
                {
                    "slug": "canonical",
                    "title": "Canonical",
                    "aliases": ["Known Alias", "Canonical"],
                    "identity_aliases": ["Known Alias"],
                }
            ],
        },
        "Document",
        "source.md",
    )
    assert artifact["core_concepts"][0]["aliases"] == ["Known Alias", "Canonical"]
    assert artifact["core_concepts"][0]["identity_aliases"] == ["Known Alias"]


def test_replay_preserves_proven_identity_aliases() -> None:
    normalized = _merge_contributions(
        [
            {"concept": {"slug": "same", "title": "Same", "aliases": [], "identity_aliases": ["First"]}},
            {"concept": {"slug": "same", "title": "Same", "aliases": [], "identity_aliases": ["Second"]}},
        ]
    )
    assert normalized["concept_ledger"][0]["identity_aliases"] == ["First", "Second"]


def test_invalidation_removes_stale_evidence_but_not_identity_provenance() -> None:
    result = _without_stale(
        {
            "operation_id": "op-1",
            "concept": {
                "slug": "same",
                "identity_aliases": ["Known Alias"],
                "anchor_reference_ids": ["doc-1:B0001"],
                "evidence_claim_ids": ["op-1:ev-1"],
            },
            "evidence_units": [
                {"evidence_id": "op-1:ev-1", "anchor_reference_ids": ["doc-1:B0001"]}
            ],
        },
        {"doc-1:B0001"},
        {"op-1:ev-1"},
    )
    assert result["concept"]["identity_aliases"] == ["Known Alias"]
    assert result["concept"]["anchor_reference_ids"] == []
    assert result["evidence_units"] == []
