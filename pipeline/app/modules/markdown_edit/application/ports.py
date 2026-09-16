from typing import Protocol

from app.modules.markdown_edit.domain.entities import (
    MarkdownCreateRequest,
    MarkdownCreateResult,
    MarkdownEditRequest,
    MarkdownEditResult,
)


class MarkdownEditorPort(Protocol):
    def generate_edit(self, request: MarkdownEditRequest) -> MarkdownEditResult:
        ...

    def generate_markdown(self, request: MarkdownCreateRequest) -> MarkdownCreateResult:
        ...
