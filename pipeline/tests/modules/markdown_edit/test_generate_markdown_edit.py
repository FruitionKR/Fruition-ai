import unittest

from app.modules.markdown_edit.application.generate_markdown_edit import (
    GenerateMarkdownEditUseCase,
)
from app.modules.markdown_edit.domain.entities import (
    MarkdownEditOperation,
    MarkdownEditRequest,
    MarkdownEditResult,
    MarkdownEditTarget,
)


class FakeMarkdownEditor:
    def __init__(self, result: MarkdownEditResult) -> None:
        self.result = result
        self.requests: list[MarkdownEditRequest] = []

    def generate_edit(self, request: MarkdownEditRequest) -> MarkdownEditResult:
        self.requests.append(request)
        return self.result


class GenerateMarkdownEditUseCaseTest(unittest.TestCase):
    def test_returns_insert_after_operation_for_current_section(self) -> None:
        target = MarkdownEditTarget(type="current_section", start_line=1, end_line=3)
        editor = FakeMarkdownEditor(
            MarkdownEditResult(
                edit=MarkdownEditOperation(
                    operation="insert_after",
                    target=target,
                    summary="문제 해결 절을 추가했습니다.",
                    replacement_markdown="## 문제 해결\n\n로그를 확인합니다.",
                )
            )
        )

        result = GenerateMarkdownEditUseCase(editor).execute(
            MarkdownEditRequest(
                instruction="이 섹션 아래에 문제 해결 절을 추가해줘.",
                markdown="# 설치\n\n설치 방법입니다.",
                target=target,
                edit_goal="other",
                edit_operation="insert_after",
            )
        )

        self.assertEqual(result.edit.operation, "insert_after")
        self.assertTrue(result.edit.replacement_markdown.startswith("## 문제 해결"))

    def test_rejects_insert_after_for_non_section_target(self) -> None:
        target = MarkdownEditTarget(type="selection", start_line=1, end_line=1)
        editor = FakeMarkdownEditor(
            MarkdownEditResult(
                edit=MarkdownEditOperation(
                    operation="insert_after",
                    target=target,
                    summary="내용을 추가했습니다.",
                    replacement_markdown="추가 내용",
                )
            )
        )

        with self.assertRaisesRegex(ValueError, "current_section"):
            GenerateMarkdownEditUseCase(editor).execute(
                MarkdownEditRequest(
                    instruction="아래에 추가해줘.",
                    markdown="본문",
                    target=target,
                    edit_goal="other",
                    edit_operation="insert_after",
                )
            )

        self.assertEqual(editor.requests, [])

    def test_inserts_at_document_end_without_replacing_selected_content(self) -> None:
        requested_target = MarkdownEditTarget(type="selection", start_line=2, end_line=2)
        document_target = MarkdownEditTarget(type="whole_document", start_line=1, end_line=4)
        editor = FakeMarkdownEditor(
            MarkdownEditResult(
                edit=MarkdownEditOperation(
                    operation="insert_after",
                    target=document_target,
                    summary="문서 아래에 내용을 추가했습니다.",
                    replacement_markdown="## 추가 내용\n\n새 본문",
                )
            )
        )

        result = GenerateMarkdownEditUseCase(editor).execute(
            MarkdownEditRequest(
                instruction="그 부분을 이 문서 아래에 추가해줘",
                markdown="# 제목\n기존 본문\n\n끝",
                target=requested_target,
                edit_goal="other",
                edit_operation="insert_after",
                edit_destination="document_end",
            )
        )

        self.assertEqual(editor.requests[0].target, document_target)
        self.assertEqual(result.edit.requested_target, requested_target)
        self.assertEqual(result.edit.actual_target, document_target)
        self.assertEqual(result.edit.operation, "insert_after")

    def test_returns_replace_operation_for_requested_target(self) -> None:
        target = MarkdownEditTarget(type="selection", start_line=2, end_line=4)
        editor = FakeMarkdownEditor(
            MarkdownEditResult(
                edit=MarkdownEditOperation(
                    operation="replace",
                    target=target,
                    summary="선택 영역을 표로 바꿨습니다.",
                    replacement_markdown="| 항목 | 내용 |\n| --- | --- |\n| A | B |",
                )
            )
        )
        use_case = GenerateMarkdownEditUseCase(editor)

        result = use_case.execute(
            MarkdownEditRequest(
                instruction="표로 바꿔줘",
                markdown="# 제목\n\nA: B\n추가 설명",
                target=target,
            )
        )

        self.assertEqual(result.edit.target, target)
        self.assertEqual(result.edit.operation, "replace")
        self.assertIn("| 항목 | 내용 |", result.edit.replacement_markdown)
        self.assertEqual(editor.requests[0].instruction, "표로 바꿔줘")

    def test_allows_actual_target_to_expand_beyond_requested_target(self) -> None:
        requested_target = MarkdownEditTarget(type="selection", start_line=2, end_line=2)
        actual_target = MarkdownEditTarget(type="selection", start_line=1, end_line=3)
        use_case = GenerateMarkdownEditUseCase(
            FakeMarkdownEditor(
                MarkdownEditResult(
                    edit=MarkdownEditOperation(
                        operation="replace",
                        target=actual_target,
                        summary="문맥을 포함해 문단을 정리했습니다.",
                        replacement_markdown="정리한 문단",
                    )
                )
            )
        )

        result = use_case.execute(
            MarkdownEditRequest(
                instruction="이 문장을 문맥에 맞게 정리해줘.",
                markdown="앞 문장\n대상 문장\n뒤 문장",
                target=requested_target,
            )
        )

        self.assertEqual(result.edit.target, actual_target)

    def test_rejects_actual_target_outside_document(self) -> None:
        requested_target = MarkdownEditTarget(type="selection", start_line=2, end_line=2)
        use_case = GenerateMarkdownEditUseCase(
            FakeMarkdownEditor(
                MarkdownEditResult(
                    edit=MarkdownEditOperation(
                        operation="replace",
                        target=MarkdownEditTarget(type="selection", start_line=1, end_line=4),
                        summary="문단을 정리했습니다.",
                        replacement_markdown="정리한 문단",
                    )
                )
            )
        )

        with self.assertRaisesRegex(ValueError, "actual_target.end_line"):
            use_case.execute(
                MarkdownEditRequest(
                    instruction="이 문장을 문맥에 맞게 정리해줘.",
                    markdown="앞 문장\n대상 문장\n뒤 문장",
                    target=requested_target,
                )
            )

    def test_rejects_actual_target_that_does_not_contain_requested_target(self) -> None:
        requested_target = MarkdownEditTarget(type="selection", start_line=1, end_line=2)
        use_case = GenerateMarkdownEditUseCase(
            FakeMarkdownEditor(
                MarkdownEditResult(
                    edit=MarkdownEditOperation(
                        operation="replace",
                        target=MarkdownEditTarget(type="selection", start_line=2, end_line=2),
                        summary="일부만 정리했습니다.",
                        replacement_markdown="둘째 문장",
                    )
                )
            )
        )

        with self.assertRaisesRegex(ValueError, "must contain"):
            use_case.execute(
                MarkdownEditRequest(
                    instruction="선택 범위를 정리해줘.",
                    markdown="첫 문장\n둘째 문장",
                    target=requested_target,
                )
            )

    def test_marks_unchanged_replace_result(self) -> None:
        target = MarkdownEditTarget(type="selection", start_line=1, end_line=1)
        use_case = GenerateMarkdownEditUseCase(
            FakeMarkdownEditor(
                MarkdownEditResult(
                    edit=MarkdownEditOperation(
                        operation="replace",
                        target=target,
                        summary="변경할 내용이 없습니다.",
                        replacement_markdown="같은 문장",
                    )
                )
            )
        )

        result = use_case.execute(
            MarkdownEditRequest(
                instruction="문장을 확인해줘.",
                markdown="같은 문장",
                target=target,
            )
        )

        self.assertFalse(result.edit.changed)

    def test_marks_crlf_replace_result_as_unchanged(self) -> None:
        target = MarkdownEditTarget(type="whole_document", start_line=1, end_line=2)
        markdown = "# 제목\r\n본문"
        use_case = GenerateMarkdownEditUseCase(
            FakeMarkdownEditor(
                MarkdownEditResult(
                    edit=MarkdownEditOperation(
                        operation="replace",
                        target=target,
                        summary="변경할 내용이 없습니다.",
                        replacement_markdown=markdown,
                    )
                )
            )
        )

        result = use_case.execute(
            MarkdownEditRequest(
                instruction="문서를 확인해줘.",
                markdown=markdown,
                target=target,
            )
        )

        self.assertFalse(result.edit.changed)

    def test_rejects_empty_replacement_markdown(self) -> None:
        target = MarkdownEditTarget(type="selection", start_line=1, end_line=1)
        use_case = GenerateMarkdownEditUseCase(
            FakeMarkdownEditor(
                MarkdownEditResult(
                    edit=MarkdownEditOperation(
                        operation="replace",
                        target=target,
                        summary="빈 결과입니다.",
                        replacement_markdown="",
                    )
                )
            )
        )

        with self.assertRaises(ValueError):
            use_case.execute(
                MarkdownEditRequest(
                    instruction="줄여줘",
                    markdown="긴 문장입니다.",
                    target=target,
                )
            )

    def test_rejects_partial_range_labeled_as_whole_document(self) -> None:
        target = MarkdownEditTarget(type="whole_document", start_line=2, end_line=2)
        editor = FakeMarkdownEditor(
            MarkdownEditResult(
                edit=MarkdownEditOperation(
                    operation="replace",
                    target=target,
                    summary="문서 전체를 정리했습니다.",
                    replacement_markdown="# 전체 문서",
                )
            )
        )
        use_case = GenerateMarkdownEditUseCase(editor)

        with self.assertRaisesRegex(ValueError, "whole_document target must cover"):
            use_case.execute(
                MarkdownEditRequest(
                    instruction="문서 전체를 정리해줘.",
                    markdown="# 제목\n\n본문",
                    target=target,
                )
            )

        self.assertEqual(editor.requests, [])


if __name__ == "__main__":
    unittest.main()
