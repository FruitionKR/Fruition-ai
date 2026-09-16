import unittest

from app.modules.markdown_edit.application.generate_markdown_document import GenerateMarkdownDocumentUseCase
from app.modules.markdown_edit.domain.entities import (
    GeneratedMarkdownDocument,
    MarkdownCreateRequest,
    MarkdownCreateResult,
)


class FakeMarkdownCreator:
    def __init__(self, result: MarkdownCreateResult) -> None:
        self.result = result
        self.requests: list[MarkdownCreateRequest] = []

    def generate_markdown(self, request: MarkdownCreateRequest) -> MarkdownCreateResult:
        self.requests.append(request)
        return self.result


class GenerateMarkdownDocumentUseCaseTest(unittest.TestCase):
    def test_returns_generated_markdown_document(self) -> None:
        creator = FakeMarkdownCreator(
            MarkdownCreateResult(
                document=GeneratedMarkdownDocument(
                    title="대화 정리",
                    summary="대화 내용을 Markdown 문서로 정리했습니다.",
                    markdown="# 대화 정리\n\n- 핵심 내용",
                )
            )
        )
        use_case = GenerateMarkdownDocumentUseCase(creator)  # type: ignore[arg-type]

        result = use_case.execute(
            MarkdownCreateRequest(
                instruction="지금까지 이야기한 내용 md로 만들어줘",
                conversation_summary="핵심 내용을 문서로 정리하기로 했다.",
            )
        )

        self.assertEqual(result.document.title, "대화 정리")
        self.assertIn("# 대화 정리", result.document.markdown)
        self.assertEqual(creator.requests[0].instruction, "지금까지 이야기한 내용 md로 만들어줘")

    def test_rejects_empty_markdown(self) -> None:
        creator = FakeMarkdownCreator(
            MarkdownCreateResult(
                document=GeneratedMarkdownDocument(
                    title="대화 정리",
                    summary="빈 결과입니다.",
                    markdown="",
                )
            )
        )
        use_case = GenerateMarkdownDocumentUseCase(creator)  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            use_case.execute(MarkdownCreateRequest(instruction="md로 만들어줘"))


if __name__ == "__main__":
    unittest.main()
