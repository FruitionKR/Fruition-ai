from copy import deepcopy

from app.modules.wiki_generation.infrastructure.source_context_merge import (
    active_source_artifact,
    source_context_blocks,
    source_page_context_normalized,
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
    assert result["summary"] == "기존 요약"


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
    assert result["summary"] == "기존 전체 요약"
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
    assert result["summary"] == ""
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
    assert result["summary"] == "기존 전체 요약"
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


def test_source_context_blocks_exposes_existing_artifact_refs_for_source_polish() -> None:
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
