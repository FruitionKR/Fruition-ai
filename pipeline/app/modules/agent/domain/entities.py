from dataclasses import dataclass, field
from typing import Literal

from app.modules.markdown_edit.domain.entities import (
    EditDestination,
    EditOperationType,
    GeneratedMarkdownDocument,
    MarkdownEditOperation,
    MarkdownEditTarget,
)
from app.modules.query.domain.entities import (
    ConversationMessage,
    OutputLanguage,
    QueryAnswer,
    ResponseLength,
)
from app.modules.skill.domain.entities import (
    SkillAuthoringMode,
    SkillAuthoringResult,
    SkillCapability,
    SkillDraftSourceRun,
    SkillScopeType,
    SkillTool,
)

AgentAction = Literal[
    "chat_answer",
    "conversation_reply",
    "markdown_edit",
    "markdown_create",
    "folder_organize",
    "folder_lookup",
    "workspace_workflow",
    "skill_authoring",
    "skill_draft_proposal",
    "clarify",
    "reject",
]
SkillMode = Literal["auto", "explicit", "off"]
RetrievalSource = Literal["none", "workspace", "web"]
DocumentOperation = Literal["none", "create", "edit"]
# 작업 탭 요청. None이면 기존(채팅 겸용) 동작이다.
AgentTurnMode = Literal["task"]
# 사용자에게 무엇을 알릴지. clarify는 되묻기 종류, reject는 거절 이유, 문서 작업은 Skill이 꺼져 있다는 안내다.
NoticeKind = Literal["chat_tab", "details", "web_disabled", "sensitive_data", "unsupported", "policy", "skill_off"]
CLARIFY_NOTICE_KINDS = frozenset({"chat_tab", "details", "web_disabled"})
REJECT_NOTICE_KINDS = frozenset({"sensitive_data", "unsupported", "policy"})
@dataclass(frozen=True)
class SkillCandidate:
    id: str
    version_id: str
    name: str
    description: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class ActiveMarkdownContext:
    markdown: str
    target: MarkdownEditTarget | None = None


@dataclass(frozen=True)
class PendingSkillProposal:
    scope_type: SkillScopeType
    name: str
    description: str
    instructions_markdown: str
    capabilities: tuple[SkillCapability, ...]
    allowed_tools: tuple[SkillTool, ...]
    # 작성 초안의 원문 필요 여부(skill_authoring.requires_source). 없으면 게시 때 규칙으로 정한다.
    requires_source: bool | None = None


@dataclass(frozen=True)
class AgentConversationContext:
    recent_conversation_summary: str | None = None
    recent_messages: tuple[ConversationMessage, ...] = ()
    reference_context: dict[str, object] = field(default_factory=dict)
    pending_skill_proposal: PendingSkillProposal | None = None


@dataclass(frozen=True)
class AgentTurnRequest:
    message: str
    workspace_id: str | None = None
    user_id: str | None = None
    conversation_context: AgentConversationContext | None = None
    active_markdown_context: ActiveMarkdownContext | None = None
    document_id: str | None = None
    base_version: int | None = None
    skill_mode: SkillMode = "auto"
    skill_id: str | None = None
    available_skills: tuple[SkillCandidate, ...] = ()
    skill_draft_sources: tuple[SkillDraftSourceRun, ...] = ()
    skill_draft_user_directives: tuple[str, ...] = ()
    skill_draft_excluded_literals: tuple[str, ...] = ()
    skill_scope_type: SkillScopeType | None = None
    skill_authoring_mode: SkillAuthoringMode = "enhance"
    skill_reference_document_ids: tuple[str, ...] = ()
    output_language: OutputLanguage | None = None
    response_length: ResponseLength | None = None
    allow_web_search: bool | None = None
    provider: str | None = None
    model: str | None = None
    mode: AgentTurnMode | None = None
    # 작업 대상 폴더(document-svc가 검증한 폴더 UUID). 폴더 정리 범위·새 문서 위치. None이면 루트·전체다.
    folder_id: str | None = None


@dataclass(frozen=True)
class AgentTurnRoute:
    action: AgentAction
    confidence: float
    reason: str
    edit_goal: str | None = None
    edit_operation: EditOperationType | None = None
    edit_destination: EditDestination | None = None
    selected_skill_id: str | None = None
    skill_candidates: tuple[str, ...] = ()
    retrieval_source: RetrievalSource = "none"
    document_operation: DocumentOperation = "none"
    persist: bool = False
    required_capabilities: tuple[SkillCapability, ...] = ()
    notice_kind: NoticeKind | None = None


@dataclass(frozen=True)
class AgentTurnResult:
    action: AgentAction
    route: AgentTurnRoute
    query_answer: QueryAnswer | None = None
    edit: MarkdownEditOperation | None = None
    source_markdown_sha256: str | None = None
    generated_markdown: GeneratedMarkdownDocument | None = None
    message: str | None = None
    skill_candidates: tuple[SkillCandidate, ...] = ()
    run_id: str | None = None
    run_status: str | None = None
    skill_authoring_result: SkillAuthoringResult | None = None
    updated_conversation_summary: str | None = None
