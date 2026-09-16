from app.modules.wiki_generation.infrastructure.extract import MarkdownBlockExtractor
from app.modules.wiki_generation.infrastructure.normalize import SemanticNormalizer
from app.modules.wiki_generation.infrastructure.packet import SemanticPacketBuilder


CHAT_MARKDOWN = "# Chat Export\n\nQ : LangSmith 연결은 어디서 봐?\nA : traces에서 확인한다.\n"
CHAT_BLOCKS = [
    {
        "block_id": "chat_session_1:pair_1",
        "text": "Q : LangSmith 연결은 어디서 봐?\nA : traces에서 확인한다.",
    }
]


def _extract():
    return MarkdownBlockExtractor().blocks_from_records(
        CHAT_BLOCKS,
        text=CHAT_MARKDOWN,
        source_path="chat.md",
        fallback_title="chat",
    )


def test_chat_block_record_becomes_source_block_anchor() -> None:
    document, blocks = _extract()
    packets = SemanticPacketBuilder().build(document.document_id, blocks)

    assert blocks[0].block_id == "chat_session_1:pair_1"
    assert blocks[0].source_reference_id == "chat_session_1:pair_1"
    assert blocks[0].text == "Q : LangSmith 연결은 어디서 봐? A : traces에서 확인한다."
    assert packets[0].text.startswith("[chat_session_1:pair_1] Q :")


def test_chat_markdown_does_not_carry_the_anchor() -> None:
    """본문에 id가 없어도 provenance는 블록 배열로 유지된다."""
    _, blocks = _extract()

    assert "chat_session_1:pair_1" not in CHAT_MARKDOWN
    assert "chat_session_1" not in blocks[0].text


def test_semantic_normalizer_accepts_chat_pair_anchor_without_rewriting() -> None:
    document, blocks = _extract()
    note = {
        "chunk_id": "chunk_0001",
        "semantic_summary": "LangSmith traces 확인 방법을 설명한다.",
        "key_points": [
            {"text": "traces에서 LangSmith 연결을 확인한다.", "anchor_block_ids": ["chat_session_1:pair_1"]}
        ],
        "core_concepts": [
            {
                "title": "LangSmith Traces",
                "slug_hint": "langsmith-traces",
                "definition": "LangSmith traces는 실행 확인에 쓰인다.",
                "why_page_worthy": "채팅에서 확인 절차가 설명됐다.",
                "evidence_block_ids": ["chat_session_1:pair_1"],
            }
        ],
        "evidence_claims": [
            {
                "claim": "LangSmith 연결은 traces에서 확인한다.",
                "anchor_block_ids": ["chat_session_1:pair_1"],
                "related_concept_hints": ["langsmith-traces"],
                "confidence": 0.9,
            }
        ],
    }

    normalized = SemanticNormalizer(document, blocks).normalize_notes([note])

    assert normalized["warnings"] == []
    assert normalized["concept_ledger"][0]["anchor_reference_ids"] == ["chat_session_1:pair_1"]
    assert normalized["evidence_units"][0]["anchor_reference_ids"] == ["chat_session_1:pair_1"]
