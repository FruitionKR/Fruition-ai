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
NoticeKind = Literal[
    "chat_tab", "details", "web_disabled", "attachment_target", "sensitive_data", "unsupported",
    "display_unsupported", "policy", "skill_off",
]
# 되묻기 이유 코드. message 문구가 바뀌어도 화면이 이 값으로 갈래를 정한다.
# markdown_document_required: 요청에 편집할 Markdown 문맥이 없다. 문서를 안 열었거나 편집 문맥을 못 보낸 경우다.
# attachment_target_required: 첨부 중 어느 문서를 고칠지 알 수 없다. 화면은 첨부 칩을 유지한다.
ClarifyReason = Literal["markdown_document_required", "attachment_target_required"]
CLARIFY_NOTICE_KINDS = frozenset({"chat_tab", "details", "web_disabled", "attachment_target"})
REJECT_NOTICE_KINDS = frozenset({"sensitive_data", "unsupported", "display_unsupported", "policy"})
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
    # 사용자가 이 turn에 첨부한 문서(document-svc가 검증한 id, 최대 3개). 라우터가 고른 문서만 편집 대상이고 나머지는 근거다.
    reference_document_ids: tuple[str, ...] = ()
    # 중복을 뺀 첨부와 같은 순서의 제목. 라우터 입력으로만 쓴다. 읽지 못한 문서는 빈 문자열이다.
    reference_document_titles: tuple[str, ...] = ()
    # 같은 순서의 편집 가능 여부. 모르면(제목을 못 읽음, 구 document-svc) True로 두어 본문을 읽는 단계의 판정에 맡긴다.
    reference_document_editable: tuple[bool, ...] = ()
    # 첨부 턴에 열린 문서의 제목. 라우터 입력으로만 쓴다. 읽지 못했으면 빈 문자열이다.
    active_document_title: str = ""
    output_language: OutputLanguage | None = None
    response_length: ResponseLength | None = None
    allow_web_search: bool | None = None
    provider: str | None = None
    model: str | None = None
    mode: AgentTurnMode | None = None
    # 작업 대상 폴더(document-svc가 검증한 폴더 UUID). 폴더 정리 범위·새 문서 위치. None이면 루트·전체다.
    folder_id: str | None = None
    # 편집 문맥 없이 보기 권한으로 연 문서(document-svc가 확인한 id). 첨부에 있으면 "이 문서"는 그 문서다. 구 document-svc는 보내지 않는다.
    read_only_document_id: str | None = None


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
    # 고칠 첨부의 1부터 시작하는 순번. 비어 있으면 첨부는 근거다.
    attached_edit_targets: tuple[int, ...] = ()
    # 첨부와 함께 열린 문서도 고친다. attached_edit_targets가 있을 때만 쓴다.
    include_active_markdown: bool = False
    # 언어를 되물을 때 열려 있던 문서. document-svc가 다음 턴 agent_route로 돌려주어, 같은 문서일 때만 답을 앞선 요청에 잇는다.
    clarified_document_id: str | None = None


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
    clarify_reason: ClarifyReason | None = None
