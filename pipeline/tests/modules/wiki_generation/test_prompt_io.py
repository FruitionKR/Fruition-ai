from app.modules.wiki_generation.domain.entities import SemanticPacket, SourceBlock
from app.modules.wiki_generation.infrastructure.prompt_io import collect_concept_source_blocks, render_semantic_user_prompt


def test_render_semantic_user_prompt_includes_existing_source_page_markdown_only() -> None:
    packet = SemanticPacket(
        chunk_id="chunk_0001",
        document_id="chat_doc",
        block_ids=["chat_session_1:pair_1"],
        text="[chat_session_1:pair_1] LangSmith traces run 확인 절차를 설명했다.",
    )
    source_context = {
        "source_markdown": "# Existing Chat Source\n\nLangSmith traces 기존 설명.",
        "section_candidates": [{"term": "Traces", "slug": "traces", "context": "run 확인 화면"}],
        "mentions": [{"term": "Run", "slug": "run", "context": "실행 단위"}],
        "evidence_claims": [{"claim": "prompt에는 들어가면 안 됨"}],
    }

    prompt = render_semantic_user_prompt(packet, source_context)

    assert "EXISTING SOURCE PAGE MARKDOWN" in prompt
    assert "# Existing Chat Source" in prompt
    assert '"section_candidates"' not in prompt
    assert '"slug": "traces"' not in prompt
    assert "prompt에는 들어가면 안 됨" not in prompt
    assert "[chat_session_1:pair_1]" in prompt
    assert "SOURCE BLOCKS:" in prompt


def test_collect_concept_source_blocks_uses_accumulated_source_key_point_refs() -> None:
    concept = {
        "slug": "traces",
        "display_reference_ids": ["chat:pair_003"],
        "anchor_reference_ids": ["chat:pair_003"],
        "mention_reference_ids": [],
    }
    blocks = [
        SourceBlock("chat_doc", "chat:pair_001", "chat:pair_001", "기존 traces 설명", 0, 0),
        SourceBlock("chat_doc", "chat:pair_003", "chat:pair_003", "새 traces 설명", 0, 0),
    ]

    selected = collect_concept_source_blocks(
        concept,
        [],
        blocks,
        source_key_points=[
            {
                "text": "traces는 run을 추적한다",
                "anchor_reference_ids": ["chat:pair_001", "chat:pair_003"],
            }
        ],
    )

    assert [block.block_id for block in selected] == ["chat:pair_003", "chat:pair_001"]
