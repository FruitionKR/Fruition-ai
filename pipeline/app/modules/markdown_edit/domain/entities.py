from dataclasses import dataclass, field
from typing import Literal

from app.core.response_preferences import OutputLanguage

TargetType = Literal["selection", "current_section", "whole_document"]
EditOperationType = Literal["replace", "insert_after"]
EditDestination = Literal["target", "document_end"]
# 편집 결과가 갖춰야 할 Markdown 형식. 세부 분류기가 요청 문장의 의미로 고르고, 출력 검사가 이 값으로 모양을 확인한다.
EDIT_FORMATS = (
    "meeting_notes",
    "table",
    "numbered_list",
    "blockquote",
    "heading",
    "bold",
    "italic",
    "strikethrough",
    "display_math",
    "mermaid",
    "code_block",
)


@dataclass(frozen=True)
class MarkdownEditTarget:
    type: TargetType
    start_line: int
    end_line: int


@dataclass(frozen=True)
class MarkdownEditDetail:
    """라우팅(대분류) 뒤 편집 요청 문장에서 다시 고른 세부 분류. 기본값은 '세부 요구 없음'이다."""

    requested_formats: tuple[str, ...] = ()
    # 기존 내용은 그대로 두고 새 내용만 더하라는 요청인가
    addition_only: bool = False
    # 표·링크·목록·코드 같은 Markdown 구조 자체를 바꾸라는 요청인가
    structure_change: bool = False
    # 번역 요청의 목표 언어(ISO 639-1). 번역이 아니거나 알 수 없으면 None
    target_language: str | None = None


@dataclass(frozen=True)
class MarkdownEditRequest:
    instruction: str
    markdown: str
    target: MarkdownEditTarget
    workspace_id: str | None = None
    user_id: str | None = None
    conversation_summary: str | None = None
    reference_context: dict[str, object] | None = None
    edit_goal: str | None = None
    edit_operation: EditOperationType = "replace"
    edit_destination: EditDestination = "target"
    skill_instructions: str | None = None
    output_language: OutputLanguage | None = None
    detail: MarkdownEditDetail = field(default_factory=MarkdownEditDetail)


@dataclass(frozen=True)
class MarkdownEditOperation:
    operation: EditOperationType
    target: MarkdownEditTarget
    summary: str
    replacement_markdown: str
    requested_target: MarkdownEditTarget | None = None
    changed: bool = True

    @property
    def actual_target(self) -> MarkdownEditTarget:
        return self.target

    @property
    def effective_requested_target(self) -> MarkdownEditTarget:
        return self.requested_target or self.target

    @property
    def scope_expanded(self) -> bool:
        requested = self.effective_requested_target
        actual = self.actual_target
        return actual.start_line < requested.start_line or actual.end_line > requested.end_line


@dataclass(frozen=True)
class MarkdownEditResult:
    edit: MarkdownEditOperation


@dataclass(frozen=True)
class MarkdownCreateRequest:
    instruction: str
    workspace_id: str | None = None
    user_id: str | None = None
    conversation_summary: str | None = None
    reference_context: dict[str, object] | None = None
    skill_instructions: str | None = None
    output_language: OutputLanguage | None = None


@dataclass(frozen=True)
class GeneratedMarkdownDocument:
    title: str
    summary: str
    markdown: str


@dataclass(frozen=True)
class MarkdownCreateResult:
    document: GeneratedMarkdownDocument
