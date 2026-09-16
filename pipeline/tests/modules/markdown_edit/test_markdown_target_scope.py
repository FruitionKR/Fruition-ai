import unittest

from app.modules.markdown_edit.domain.entities import MarkdownEditOperation, MarkdownEditTarget
from app.modules.markdown_edit.domain.markdown_target_scope import (
    MarkdownTargetBoundaryError,
    apply_markdown_edit,
    build_markdown_target_scope,
)
from app.modules.markdown_edit.infrastructure.markdown_source_range import validate_markdown_target_boundary


class MarkdownTargetScopeTest(unittest.TestCase):
    def test_applies_replace_and_insert_after_to_full_markdown(self) -> None:
        target = MarkdownEditTarget(type="current_section", start_line=2, end_line=2)

        replaced = apply_markdown_edit(
            "# 제목\n기존\n마지막",
            MarkdownEditOperation("replace", target, "replace", "변경"),
        )
        inserted = apply_markdown_edit(
            "# 제목\n기존\n마지막",
            MarkdownEditOperation("insert_after", target, "insert", "추가"),
        )

        self.assertEqual(replaced, "# 제목\n변경\n마지막")
        self.assertEqual(inserted, "# 제목\n기존\n추가\n마지막")

    def test_slices_selection_and_limits_read_only_context(self) -> None:
        markdown = "\n".join(f"line {line}" for line in range(1, 11))
        target = MarkdownEditTarget(type="selection", start_line=5, end_line=6)

        scope = build_markdown_target_scope(markdown, target, context_lines=2)

        self.assertEqual(scope.markdown, "line 5\nline 6")
        self.assertEqual(scope.context_before, "line 3\nline 4")
        self.assertEqual(scope.context_after, "line 7\nline 8")

    def test_keeps_whole_document_exactly(self) -> None:
        markdown = "첫 줄\r\n둘째 줄\r\n"
        target = MarkdownEditTarget(type="whole_document", start_line=1, end_line=2)

        scope = build_markdown_target_scope(markdown, target, context_lines=2)

        self.assertEqual(scope.markdown, markdown)
        self.assertEqual(scope.context_before, "")
        self.assertEqual(scope.context_after, "")

    def test_keeps_crlf_in_selection_and_context(self) -> None:
        markdown = "첫 줄\r\n둘째 줄\r\n셋째 줄\r\n넷째 줄"
        target = MarkdownEditTarget(type="selection", start_line=2, end_line=3)

        scope = build_markdown_target_scope(markdown, target, context_lines=1)

        self.assertEqual(scope.markdown, "둘째 줄\r\n셋째 줄")
        self.assertEqual(scope.context_before, "첫 줄")
        self.assertEqual(scope.context_after, "넷째 줄")

    def test_rejects_target_past_document_end(self) -> None:
        target = MarkdownEditTarget(type="selection", start_line=2, end_line=3)

        with self.assertRaisesRegex(ValueError, "line count"):
            build_markdown_target_scope("첫 줄\n둘째 줄", target, context_lines=2)

    def test_allows_selecting_trailing_empty_line(self) -> None:
        target = MarkdownEditTarget(type="selection", start_line=2, end_line=2)

        scope = build_markdown_target_scope("첫 줄\n", target, context_lines=1)

        self.assertEqual(scope.markdown, "")
        self.assertEqual(scope.context_before, "첫 줄")

    def test_rejects_selection_inside_protected_multiline_structures(self) -> None:
        fixtures = (
            ("앞\n```bash\necho ok\n```\n뒤", 3, "fence"),
            ("앞\n| A | B |\n| --- | --- |\n| 1 | 2 |\n뒤", 4, "table_open"),
            ("---\ntitle: guide\n---\n본문", 2, "frontmatter"),
            ("본문[^1]\n\n[^1]: 첫 줄\n  둘째 줄", 4, "footnote_definition"),
            ("앞\n$$\nE = mc^2\n$$\n뒤", 3, "display_math"),
        )

        for markdown, selected_line, structure in fixtures:
            with self.subTest(structure=structure):
                target = MarkdownEditTarget(type="selection", start_line=selected_line, end_line=selected_line)
                with self.assertRaises(MarkdownTargetBoundaryError) as raised:
                    validate_markdown_target_boundary(markdown, target)
                self.assertEqual(raised.exception.structure, structure)

    def test_allows_selection_containing_complete_protected_structure(self) -> None:
        markdown = "앞\n```bash\necho ok\n```\n뒤"
        target = MarkdownEditTarget(type="selection", start_line=2, end_line=4)

        validate_markdown_target_boundary(markdown, target)

    def test_property_rejects_every_partial_protected_block_range(self) -> None:
        blocks = (
            ("```bash\necho ok\n```", "fence"),
            ("| A | B |\n| --- | --- |\n| 1 | 2 |", "table_open"),
            ("$$\nE = mc^2\n$$", "display_math"),
        )

        for block, structure in blocks:
            for prefix_count in range(3):
                for suffix_count in range(3):
                    lines = [
                        *(f"앞 {index}" for index in range(prefix_count)),
                        *block.splitlines(),
                        *([""] if suffix_count else []),
                        *(f"뒤 {index}" for index in range(suffix_count)),
                    ]
                    markdown = "\n".join(lines)
                    block_start = prefix_count + 1
                    block_end = block_start + len(block.splitlines()) - 1

                    for start_line in range(1, len(lines) + 1):
                        for end_line in range(start_line, len(lines) + 1):
                            target = MarkdownEditTarget(
                                type="selection",
                                start_line=start_line,
                                end_line=end_line,
                            )
                            overlaps = start_line <= block_end and end_line >= block_start
                            contains = start_line <= block_start and end_line >= block_end
                            if overlaps and not contains:
                                with self.assertRaises(MarkdownTargetBoundaryError) as raised:
                                    validate_markdown_target_boundary(markdown, target)
                                self.assertEqual(raised.exception.structure, structure)
                            else:
                                validate_markdown_target_boundary(markdown, target)


if __name__ == "__main__":
    unittest.main()
