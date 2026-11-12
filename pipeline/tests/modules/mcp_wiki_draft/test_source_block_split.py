from app.modules.mcp_wiki_draft.source_block_registry import split_source_markdown
from app.modules.wiki_generation.domain.text_utils import normalize_space


def test_raw_text_equals_the_line_range_even_for_an_unclosed_code_fence():
    # document는 raw_text가 원문 line_start..line_end 줄을 이은 것과 같은지 검사한다.
    markdown = "| a | b |\n  들여쓴 줄  \n```py\n\n"
    lines = markdown.split("\n")

    split = split_source_markdown("doc_x", markdown)

    assert split
    for block, raw in split:
        assert raw == "\n".join(lines[block.line_start - 1:block.line_end])
        assert normalize_space(raw) == block.text
