from copy import deepcopy

from app.modules.wiki_generation.infrastructure.source_context_merge import (
    active_source_artifact,
    source_context_blocks,
    source_page_context_parts,
    source_page_context_normalized,
)
from app.modules.wiki_ingestion.application.concept_contribution import (
    build_concept_contributions,
)


def test_active_source_artifact_removes_invalidated_block_contributions() -> None:
    artifact = {
        "summary": "기존 요약",
        "key_points": [
            {"text": "유지", "evidence_block_ids": ["B0001"]},
            {"text": "삭제", "evidence_block_ids": ["B0002"]},
        ],
        "categories": [
            {"term": "유지 분류", "evidence_block_ids": ["B0001"]},
            {"term": "삭제 분류", "evidence_block_ids": ["B0002"]},
        ],
        "observations": [],
        "core_concepts": [
            {"term": "일부 유지", "evidence_block_ids": ["B0001", "B0002"]}
        ],
        "section_candidates": [],
        "mentions": [],
        "evidence_claims": [
            {"claim": "삭제 주장", "anchor_reference_ids": ["B0002"]}
        ],
    }

    result = active_source_artifact(
        artifact,
        ["B0001"],
        current_has_blocks=True,
    )

    assert result is not None
    assert [item["text"] for item in result["key_points"]] == ["유지"]
    assert [item["term"] for item in result["categories"]] == ["유지 분류"]
    assert result["core_concepts"][0]["evidence_block_ids"] == ["B0001"]
    assert result["evidence_claims"] == []
    assert "summary" not in result


def test_active_source_artifact_preserves_unreferenced_contributions() -> None:
    artifact = {
        "summary": "기존 전체 요약",
        "key_points": [
            {"text": "유지", "evidence_block_ids": ["B0001"]},
            {"text": "삭제", "evidence_block_ids": ["B0002"]},
        ],
        "categories": ["legacy-category"],
        "observations": [{"summary": "참조 없는 기존 관찰"}],
    }

    result = active_source_artifact(
        artifact,
        ["B0001"],
        current_has_blocks=True,
    )

    assert result is not None
    assert "summary" not in result
    assert result["categories"] == ["legacy-category"]
    assert result["observations"] == [{"summary": "참조 없는 기존 관찰"}]


def test_active_source_artifact_clears_all_contributions_without_active_blocks() -> None:
    artifact = {
        "summary": "기존 요약",
        "key_points": [{"text": "기존", "evidence_block_ids": ["B0001"]}],
        "categories": ["legacy-category"],
    }

    result = active_source_artifact(
        artifact,
        [],
        current_has_blocks=False,
    )

    assert result is not None
    assert "summary" not in result
    assert result["key_points"] == []
    assert result["categories"] == []


def test_active_source_artifact_preserves_unreferenced_context_when_all_blocks_changed() -> None:
    artifact = {
        "summary": "기존 전체 요약",
        "key_points": [{"text": "수정 전 핵심", "evidence_block_ids": ["B0001"]}],
        "categories": ["legacy-category"],
    }

    result = active_source_artifact(
        artifact,
        [],
        current_has_blocks=True,
    )

    assert result is not None
    assert "summary" not in result
    assert result["key_points"] == []
    assert result["categories"] == ["legacy-category"]


def test_source_page_context_normalized_preserves_existing_source_metadata_without_demoting_new_core() -> None:
    normalized = {
        "semantic_notes": [
            {"chunk_id": "new", "semantic_summary": "새 요약", "key_points": [{"text": "새 핵심", "anchor_reference_ids": ["B0005"]}]}
        ],
        "concept_ledger": [
            {"slug": "langsmith-traces", "title": "LangSmith Traces"}
        ],
        "categories": [{"name": "new-category"}],
        "observations": [{"summary": "새 observation", "anchor_reference_ids": ["B0005"]}],
        "evidence_units": [],
        "section_candidates": [
            {"title": "LangSmith Traces - 디버깅 핵심 단위", "slug": "langsmith-traces-debugging", "context": "새 core와 의미상 겹침", "anchor_reference_ids": ["B0005"]}
        ],
        "mentions": [
            {"name": "LangSmith Traces 디버깅", "slug": "langsmith-traces-debugging", "context": "새 core와 의미상 겹침", "anchor_reference_ids": ["B0005"]}
        ],
    }
    source_artifact = {
        "document_id": "chat_doc",
        "core_concepts": [
            {"term": "기존 Core", "slug": "existing-core", "evidence_block_ids": ["B0001"]}
        ],
        "key_points": [{"text": "기존 핵심", "evidence_block_ids": ["B0001"]}],
        "categories": ["existing-category"],
        "observations": [{"summary": "기존 observation", "anchor_reference_ids": ["B0001"]}],
        "evidence_claims": [{"claim": "기존 claim", "anchor_reference_ids": ["B0001"]}],
        "section_candidates": [
            {"term": "LangSmith Traces", "slug": "langsmith-traces", "context": "승격 대상", "evidence_block_ids": ["B0002"]},
            {"term": "남는 Section", "slug": "remaining-section", "context": "아직 후보", "evidence_block_ids": ["B0003"]},
        ],
        "mentions": [
            {"term": "남는 Mention", "slug": "remaining-mention", "context": "단순 언급", "evidence_block_ids": ["B0004"]}
        ],
    }

    result = source_page_context_normalized(normalized, source_artifact)

    assert result["semantic_notes"][0]["chunk_id"] == "existing_source_page"
    assert result["semantic_notes"][0]["key_points"][0]["text"] == "기존 핵심"
    assert result["semantic_notes"][1]["chunk_id"] == "new"
    assert [item["name"] for item in result["categories"]] == ["existing-category", "new-category"]
    assert [item["summary"] for item in result["observations"]] == ["기존 observation", "새 observation"]
    assert [item["claim"] for item in result["evidence_units"]] == ["기존 claim"]
    assert [item["slug"] for item in result["concept_ledger"]] == ["existing-core", "langsmith-traces"]
    assert [item["slug"] for item in result["section_candidates"]] == ["remaining-section"]
    assert [item["slug"] for item in result["mentions"]] == ["remaining-mention"]


def test_source_page_context_normalized_keeps_new_core_not_in_existing_source_artifact() -> None:
    normalized = {
        "concept_ledger": [
            {"slug": "new-core", "title": "New Core"}
        ],
        "section_candidates": [],
        "mentions": [],
    }
    source_artifact = {
        "document_id": "chat_doc",
        "core_concepts": [],
        "section_candidates": [],
        "mentions": [],
    }

    result = source_page_context_normalized(normalized, source_artifact)

    assert [item["slug"] for item in result["concept_ledger"]] == ["new-core"]


def test_source_context_remaps_only_incoming_references_when_slug_moves() -> None:
    normalized = {
        "concept_ledger": [{"slug": "c", "title": "C#", "identity_aliases": []}],
        "evidence_units": [
            {"evidence_id": "new", "claim": "Incoming sharp claim", "related_concept_slugs": ["c"]},
        ],
        "observations": [{"summary": "new observation", "related_concept_hints": ["c"]}],
        "entry_changes": {"c": [{"op": "add", "slug": "c"}]},
        "entry_references": [{"source": "concept:c", "target": "concept:c"}],
        "alias_concept_links": [{"source": "c", "target": "c"}],
        "concept_slug_map": {"c": "c"},
        "hint_slug_map": {"C#": "c"},
        "concept_resolutions": [
            {"incoming_slug": "c", "canonical_slug": "c", "link_targets": ["c"], "link_confidences": {"c": 0.9}},
        ],
        "concept_update_decisions": [{"concept_slug": "c"}],
        "section_candidates": [],
        "mentions": [],
    }
    source_artifact = {
        "document_id": "doc-1",
        "core_concepts": [{"term": "C", "slug": "c", "aliases": []}],
        "evidence_claims": [
            {"evidence_id": "old", "claim": "Historical C claim", "related_concept_slugs": ["c"]},
        ],
        "section_candidates": [],
        "mentions": [],
        "source_markdown": "[[c|C]]",
    }

    result = source_page_context_normalized(normalized, source_artifact)

    assert [(item["title"], item["slug"]) for item in result["concept_ledger"]] == [("C", "c"), ("C#", "c-2")]
    assert {item["evidence_id"]: item["related_concept_slugs"] for item in result["evidence_units"]} == {
        "old": ["c"],
        "new": ["c-2"],
    }
    assert result["observations"][0]["related_concept_hints"] == ["c-2"]
    assert result["entry_changes"] == {"c-2": [{"op": "add", "slug": "c-2"}]}
    assert result["entry_references"] == [{"source": "concept:c-2", "target": "concept:c-2"}]
    assert result["alias_concept_links"] == [{"source": "c-2", "target": "c-2"}]
    assert result["concept_slug_map"] == {"c": "c-2"}
    assert result["hint_slug_map"] == {"C#": "c-2"}
    assert result["concept_resolutions"] == [{
        "incoming_slug": "c-2",
        "canonical_slug": "c-2",
        "link_targets": ["c-2"],
        "link_confidences": {"c-2": 0.9},
    }]
    assert result["concept_update_decisions"] == [{"concept_slug": "c-2"}]
    assert result["existing_source_context"]["source_markdown"] == "[[c|C]]"


def test_source_context_keeps_converging_entries_links_and_strongest_confidence() -> None:
    normalized = {
        "document": {"document_id": "doc-new"},
        "concept_ledger": [
            {
                "slug": "alpha", "title": "Alpha",
                "aliases": ["Display A", "Shared Display"],
                "identity_aliases": ["Alias A", "Shared Alias"],
                "tags": ["tag-a", "shared"], "definition": "A", "why_page_worthy": "short",
                "anchor_reference_ids": ["A1"], "mention_reference_ids": ["A1"],
                "display_reference_ids": ["A1"], "source_document_ids": ["doc-new"],
                "evidence_claim_ids": ["ev-a"], "importance_score": 0.4,
            },
            {
                "slug": "beta", "title": "Beta",
                "aliases": ["Display B", "Shared Display"],
                "identity_aliases": ["Alias B", "Shared Alias"],
                "tags": ["tag-b", "shared"], "definition": "Beta definition",
                "why_page_worthy": "the longer reason",
                "anchor_reference_ids": ["B1", "A1"], "mention_reference_ids": ["B1"],
                "display_reference_ids": ["B1"], "source_document_ids": ["doc-new"],
                "evidence_claim_ids": ["ev-b"], "importance_score": 0.6,
            },
        ],
        "entry_changes": {
            "alpha": [{"op": "add", "entry_id": "entry:a", "slug": "alpha"}],
            "beta": [{"op": "add", "entry_id": "entry:b", "slug": "beta"}],
        },
        "links": [
            {"source": "concept:alpha", "target": "concept:outside", "relation": "related_to"},
            {"source": "concept:beta", "target": "concept:outside", "relation": "supports"},
        ],
        "concept_resolutions": [{
            "incoming_slug": "alpha",
            "canonical_slug": "alpha",
            "link_targets": ["alpha", "beta"],
            "link_confidences": {"alpha": 0.7, "beta": 0.9},
        }],
        "evidence_units": [
            {
                "evidence_id": "ev-a", "claim": "alpha evidence", "anchor_reference_ids": ["A1"],
                "related_concept_slugs": ["alpha"], "source_document_id": "doc-new",
            },
            {
                "evidence_id": "ev-b", "claim": "beta evidence", "anchor_reference_ids": ["B1"],
                "related_concept_slugs": ["beta"], "source_document_id": "doc-new",
            },
        ],
        "observations": [],
        "section_candidates": [],
        "mentions": [],
    }
    source_artifact = {
        "document_id": "doc-old",
        "core_concepts": [{
            "term": "Canonical",
            "slug": "canonical",
            "identity_aliases": ["Alpha", "Beta"],
        }],
        "evidence_claims": [],
        "section_candidates": [],
        "mentions": [],
        "source_markdown": "[[canonical]]",
    }

    merged, incoming = source_page_context_parts(normalized, source_artifact)
    contributions = build_concept_contributions(
        operation_id="op-new",
        normalized=incoming,
        source_blocks=[{"block_id": "A1", "text": "a"}, {"block_id": "B1", "text": "b"}],
        links=incoming["links"],
    )

    assert [item["slug"] for item in merged["concept_ledger"]] == ["canonical"]
    [concept] = incoming["concept_ledger"]
    assert concept["slug"] == "canonical"
    assert concept["aliases"] == ["Display A", "Shared Display", "Display B", "Beta"]
    assert concept["identity_aliases"] == ["Alias A", "Shared Alias", "Alias B"]
    assert concept["tags"] == ["tag-a", "shared", "tag-b"]
    assert concept["anchor_reference_ids"] == ["A1", "B1"]
    assert concept["mention_reference_ids"] == ["A1", "B1"]
    assert concept["display_reference_ids"] == ["A1", "B1"]
    assert concept["source_document_ids"] == ["doc-new"]
    assert concept["evidence_claim_ids"] == ["ev-a", "ev-b"]
    assert concept["importance_score"] == 1.0
    assert concept["mention_count"] == 2
    assert concept["definition"] == "Beta definition"
    assert concept["why_page_worthy"] == "the longer reason"
    assert [change["entry_id"] for change in incoming["entry_changes"]["canonical"]] == ["entry:a", "entry:b"]
    assert incoming["concept_resolutions"][0]["link_targets"] == ["canonical"]
    assert incoming["concept_resolutions"][0]["link_confidences"] == {"canonical": 0.9}
    contribution = contributions["canonical"]
    assert contribution["concept"]["identity_aliases"] == ["Alias A", "Shared Alias", "Alias B"]
    assert contribution["concept"]["evidence_claim_ids"] == ["op-new:ev-a", "op-new:ev-b"]
    assert [unit["evidence_id"] for unit in contribution["evidence_units"]] == ["op-new:ev-a", "op-new:ev-b"]
    assert [block["block_id"] for block in contribution["source_blocks"]] == ["A1", "B1"]
    assert [link["relation"] for link in contribution["links"]] == ["related_to", "supports"]
    assert [change["entry_id"] for change in contribution["entry_changes"]] == ["entry:a", "entry:b"]


def test_source_page_context_normalized_preserves_category_and_observation_semantics() -> None:
    normalized = {
        "categories": [
            {"name": "C++", "anchor_reference_ids": ["B0002"]},
            {"name": "C", "anchor_reference_ids": ["B0003"]},
        ],
        "observations": [
            {
                "type": "qa_episode",
                "title": "같은 제목",
                "query_text": "같은 질문",
                "summary": "같은 요약",
                "claims": ["같은 주장"],
                "related_concept_hints": ["같은 개념"],
                "anchor_reference_ids": ["B0002"],
            },
            {
                "type": "qa_episode",
                "title": "다른 제목",
                "query_text": "같은 질문",
                "summary": "같은 요약",
                "claims": ["같은 주장"],
                "related_concept_hints": ["같은 개념"],
                "anchor_reference_ids": ["B0003"],
            },
        ],
    }
    source_artifact = {
        "categories": [{"name": "C++", "evidence_block_ids": ["B0001"]}],
        "observations": [
            {
                "observation_id": "O001",
                "type": "qa_episode",
                "title": "같은 제목",
                "query_text": "같은 질문",
                "summary": "같은 요약",
                "claims": ["같은 주장"],
                "related_concept_hints": ["같은 개념"],
                "evidence_block_ids": ["B0001"],
            }
        ],
    }

    result = source_page_context_normalized(normalized, source_artifact)

    assert [item["name"] for item in result["categories"]] == ["C++", "C"]
    assert result["categories"][0]["anchor_reference_ids"] == ["B0001", "B0002"]
    assert len(result["observations"]) == 2
    assert result["observations"][0]["anchor_reference_ids"] == ["B0001", "B0002"]
    assert result["observations"][1]["title"] == "다른 제목"


def test_source_page_context_normalized_is_stable_and_does_not_alias_inputs() -> None:
    normalized = {
        "categories": [{"name": "C++", "anchor_reference_ids": ["B0002"]}],
        "observations": [{"summary": "관찰", "anchor_reference_ids": ["B0002"]}],
    }
    source_artifact = {
        "categories": ["C++"],
        "observations": [{"summary": "관찰", "anchor_reference_ids": ["B0001"]}],
    }

    result = source_page_context_normalized(normalized, source_artifact)
    snapshot = deepcopy(result)
    result["categories"][0]["anchor_reference_ids"].append("mutated")
    assert normalized["categories"][0]["anchor_reference_ids"] == ["B0002"]
    assert source_artifact["observations"][0]["anchor_reference_ids"] == ["B0001"]
    assert source_page_context_normalized(normalized, source_artifact) == snapshot


def test_source_context_blocks_exposes_existing_artifact_refs_for_concept_assembly() -> None:
    source_artifact = {
        "document_id": "chat_doc",
        "key_points": [{"text": "기존 핵심", "evidence_block_ids": ["chat:pair_001"]}],
        "observations": [{"summary": "기존 observation", "anchor_reference_ids": ["chat:pair_002"]}],
        "core_concepts": [],
        "section_candidates": [],
        "mentions": [],
        "evidence_claims": [],
    }

    blocks = source_context_blocks(source_artifact)

    assert [block.block_id for block in blocks] == ["chat:pair_001", "chat:pair_002"]
    assert blocks[0].text == "기존 핵심"
