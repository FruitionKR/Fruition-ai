from app.modules.wiki_generation.infrastructure.extract import MarkdownBlockExtractor
from app.modules.wiki_generation.infrastructure.normalize import SemanticNormalizer


def test_null_list_fields_from_llm_are_treated_as_empty() -> None:
    # 패킷 하나가 빈 목록 대신 null을 돌려줘도 문서 전체 인제스트가 실패하지 않아야 한다.
    document, blocks = MarkdownBlockExtractor().blocks_from_records(
        [{"block_id": "B0001", "text": "역기전력은 회전 속도에 비례한다."}],
        text="역기전력은 회전 속도에 비례한다.\n",
        source_path="paper.md",
        fallback_title="paper",
    )
    note = {
        "chunk_id": "chunk_0001",
        "semantic_summary": "역기전력 설명",
        "key_points": [{"text": "역기전력은 속도에 비례한다.", "anchor_block_ids": None}],
        "core_concepts": None,
        "observations": [{"text": "속도 비례", "claims": None, "related_concept_hints": None}],
        "categories": None,
        "section_candidates": None,
        "mentions": None,
        "evidence_claims": [
            {
                "claim": "역기전력은 회전 속도에 비례한다.",
                "anchor_block_ids": ["B0001"],
                "related_concept_hints": None,
                "confidence": 0.9,
            }
        ],
    }

    normalized = SemanticNormalizer(document, blocks).normalize_notes([note])

    assert normalized["evidence_units"][0]["claim"] == "역기전력은 회전 속도에 비례한다."
    assert normalized["evidence_units"][0]["related_concept_slugs"] == []
