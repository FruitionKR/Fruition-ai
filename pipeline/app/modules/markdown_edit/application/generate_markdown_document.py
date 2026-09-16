from app.modules.markdown_edit.application.ports import MarkdownEditorPort
from app.modules.markdown_edit.domain.entities import MarkdownCreateRequest, MarkdownCreateResult


class GenerateMarkdownDocumentUseCase:
    def __init__(self, editor: MarkdownEditorPort) -> None:
        self._editor = editor

    def execute(self, request: MarkdownCreateRequest) -> MarkdownCreateResult:
        if not request.instruction.strip():
            raise ValueError("Create instruction is required.")

        result = self._editor.generate_markdown(request)
        if not result.document.markdown.strip():
            raise ValueError("markdown must not be empty.")
        if not result.document.title.strip():
            raise ValueError("title must not be empty.")
        if not result.document.summary.strip():
            raise ValueError("summary must not be empty.")
        return result
