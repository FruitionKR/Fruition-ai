import unittest

from app.modules.skill.domain.reference_template import extract_markdown_structure


class ReferenceTemplateTest(unittest.TestCase):
    def test_preserves_single_column_table_with_outer_pipes(self) -> None:
        markdown = "| 상태 |\n| --- |\n| 완료 |\n"

        self.assertEqual(
            extract_markdown_structure(markdown),
            "| 상태 |\n| --- |\n|  |",
        )

    def test_ignores_pipe_prose_followed_by_horizontal_rule(self) -> None:
        markdown = "일반 문장 | 부연 설명\n---\n"

        self.assertEqual(extract_markdown_structure(markdown), "")

    def test_ignores_empty_pipe_line_followed_by_horizontal_rule(self) -> None:
        markdown = "|\n---\n"

        self.assertEqual(extract_markdown_structure(markdown), "")

    def test_ignores_table_with_mismatched_column_counts(self) -> None:
        markdown = "| 상태 |\n| --- | --- |\n"

        self.assertEqual(extract_markdown_structure(markdown), "")

    def test_preserves_table_body_topology_without_cell_content(self) -> None:
        markdown = (
            "# 보고서\n"
            "| 담당자 | 상태 |\n"
            "| :--- | ---: |\n"
            "| 홍길동 | 비공개 |\n"
            "| 김철수 | 내부용 |\n"
            "표 밖 본문은 제거한다.\n"
        )

        structure = extract_markdown_structure(markdown)

        self.assertEqual(
            structure,
            "# 보고서\n"
            "| 담당자 | 상태 |\n"
            "| :--- | ---: |\n"
            "|  |  |\n"
            "|  |  |",
        )
        self.assertNotIn("홍길동", structure)
        self.assertNotIn("김철수", structure)

    def test_stops_table_body_at_ordinary_pipe_prose(self) -> None:
        markdown = (
            "| 담당자 | 상태 |\n"
            "| --- | --- |\n"
            "| 홍길동 | 비공개 |\n"
            "ordinary | prose\n"
        )

        self.assertEqual(
            extract_markdown_structure(markdown),
            "| 담당자 | 상태 |\n"
            "| --- | --- |\n"
            "|  |  |",
        )

    def test_preserves_table_body_topology_with_escaped_pipes(self) -> None:
        markdown = (
            "| 이름 \\| 설명 | 상태 |\n"
            "| --- | --- |\n"
            "| 첫 \\| 둘 | 완료 |\n"
            "| 비고 | 보류 |\n"
        )

        structure = extract_markdown_structure(markdown)

        self.assertEqual(
            structure,
            "| 이름 \\| 설명 | 상태 |\n"
            "| --- | --- |\n"
            "|  |  |\n"
            "|  |  |",
        )
        self.assertNotIn("첫 \\| 둘", structure)

    def test_stops_table_body_when_entering_fenced_block(self) -> None:
        markdown = (
            "| 담당자 | 상태 |\n"
            "| --- | --- |\n"
            "| 홍길동 | 비공개 |\n"
            "```text\n"
            "코드 | 본문\n"
            "```\n"
            "fenced | prose\n"
        )

        self.assertEqual(
            extract_markdown_structure(markdown),
            "| 담당자 | 상태 |\n"
            "| --- | --- |\n"
            "|  |  |",
        )

    def test_preserves_table_without_outer_pipes(self) -> None:
        markdown = (
            "담당자 | 상태\n"
            "--- | ---\n"
            "홍길동 | 비공개\n"
            "김철수 | 내부용\n"
        )

        self.assertEqual(
            extract_markdown_structure(markdown),
            "담당자 | 상태\n"
            "--- | ---\n"
            "|  |  |\n"
            "|  |  |",
        )

    def test_normalizes_checkbox_state_and_preserves_ordinary_list_structure(self) -> None:
        markdown = (
            "  - [ ] pending\n"
            "   - [x] done\n"
            "\t* [X] upper\n"
            "- ordinary\n"
            "1. ordered\n"
        )

        self.assertEqual(
            extract_markdown_structure(markdown),
            "  - [ ] [item]\n"
            "   - [ ] [item]\n"
            "\t* [ ] [item]\n"
            "- [item]\n"
            "1. [item]",
        )


if __name__ == "__main__":
    unittest.main()
