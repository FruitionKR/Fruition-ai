from dataclasses import dataclass
from datetime import datetime
from typing import Literal


# artifact 본문과 미리 보기 객체 하나의 상한.
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024

AgentRunStatus = Literal[
    "queued",
    "planning",
    "clarification_required",
    "awaiting_approval",
    "executing",
    "verifying",
    "completed",
    "partial_failed",
    "failed",
    "conflicted",
    "rejected",
    "cancel_requested",
    "rolling_back",
    "rollback_failed",
    "cancelled",
    "halted",
]


@dataclass(frozen=True)
class StartAgentRunContent:
    markdown: str
    purpose: Literal["create_document", "apply_document_edit"] = "create_document"
    document_id: str | None = None
    base_version: int | None = None
    target: dict[str, object] | None = None
    # 첨부 문서 수정만 채운다. preview_json이 있으면 worker가 LLM 없이 이 내용으로 계획을 만든다.
    display_name: str | None = None
    summary: str | None = None
    preview_json: str | None = None


@dataclass(frozen=True)
class StartAgentRunRequest:
    workspace_id: str
    user_id: str
    instruction: str
    action: Literal["folder_organize", "workspace_workflow"] = "folder_organize"
    skill_version_id: str | None = None
    provider: str | None = None
    model: str | None = None
    content: StartAgentRunContent | None = None
    # 첨부 문서 수정처럼 문서가 여럿인 run. 순서가 계획의 작업 순서다.
    contents: tuple[StartAgentRunContent, ...] = ()
    # 작업 대상 폴더. 폴더 정리 범위이자 새 문서를 둘 위치다. None이면 워크스페이스 전체·루트다.
    target_folder_id: str | None = None
    # 선택한 Skill의 규칙 요약(Skill 설명). 승인 화면에 그대로 보여 준다. Skill 없이 실행하면 None이다.
    rule_summary: str | None = None


@dataclass(frozen=True)
class StartAgentRunArtifact:
    id: str
    content_hash: str
    markdown: str
    purpose: Literal["create_document", "apply_document_edit"] = "create_document"
    document_id: str | None = None
    base_version: int | None = None
    target: dict[str, object] | None = None
    display_name: str | None = None
    summary: str | None = None
    preview_json: str | None = None


@dataclass(frozen=True)
class AgentRun:
    id: str
    workspace_id: str
    user_id: str
    action: str
    skill_version_id: str | None
    status: AgentRunStatus
    request_summary: str
    current_plan_id: str | None = None
    error_code: str | None = None
    clarification_question: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    provider: str | None = None
    model: str | None = None
    target_folder_id: str | None = None
    rule_summary: str | None = None
    # run을 시작한 턴의 요청자 화면 언어("ko"|"en"). 계획 요약·되묻기 문구에 쓴다. None이면 기존 동작이다.
    response_language: str | None = None
    # MCP proposal provenance. Legacy web runs keep these fields null.
    origin: str | None = None
    grant_id: str | None = None
    client_id: str | None = None
    policy_generation: int | None = None
    invocation_id: str | None = None
    spec_version: str | None = None
    tool_name: str | None = None
    proposal_request_hash: str | None = None


@dataclass(frozen=True)
class McpProposalActor:
    invocation_id: str
    grant_id: str
    user_id: str
    workspace_id: str
    client_id: str
    policy_generation: int
    origin: Literal["MCP_CLIENT"]
    spec_version: Literal["2025-11-25"]
    tool_name: Literal["propose_document_edit", "propose_plan", "propose_skill_definition"]


@dataclass(frozen=True)
class McpProposalCommand:
    actor: McpProposalActor
    idempotency_key: str
    request_hash: str
    proposal: dict[str, object]


@dataclass(frozen=True)
class McpDocumentEditContext:
    document_id: str
    workspace_id: str
    current_version: int
    content_hash: str
    current_markdown: str
    display_name: str
    parent_folder_id: str | None


@dataclass(frozen=True)
class McpPlanEditDocument:
    document_id: str
    current_version: int
    content_hash: str
    current_markdown: str
    display_name: str
    parent_folder_id: str | None


@dataclass(frozen=True)
class McpPlanContext:
    workspace_id: str
    target_folder_id: str | None
    hierarchy: tuple[dict[str, object], ...]
    edit_documents: tuple[McpPlanEditDocument, ...]


@dataclass(frozen=True)
class McpProposalResult:
    run_id: str
    plan_id: str
    plan_version: int
    operation_hash: str
    status: Literal["awaiting_approval"] = "awaiting_approval"


@dataclass(frozen=True)
class McpApprovalCommand:
    run_id: str
    workspace_id: str
    user_id: str
    plan_version: int
    operation_hash: str
    operation_ids: tuple[str, ...]
    approval_id: str
    approval_mode: Literal["EXPLICIT_WEB"]
    grant_id: str
    client_id: str
    policy_generation: int
    schema_version: Literal["mcp.agent-approval.v1"] = "mcp.agent-approval.v1"


@dataclass(frozen=True)
class McpApprovalStatus:
    status: str
    reason: str | None
    lifecycle_status: str | None
    forward_status: str | None
    settlement_status: str | None


@dataclass(frozen=True)
class McpExecutionIdentity:
    approval_id: str
    grant_id: str
    client_id: str
    policy_generation: int
    operation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpExecutionAttempt:
    attempt_id: str
    attempt: int
    status: str
    created: bool = False
    receipt: dict[str, object] | None = None


@dataclass(frozen=True)
class AgentJob:
    id: str
    run_id: str
    job_type: str
    attempt_count: int
    lease_token: str
    leased_until: datetime


@dataclass(frozen=True)
class AgentRunContext:
    run: AgentRun
    skill_instructions: str | None
    allowed_tools: tuple[str, ...]


@dataclass(frozen=True)
class DocumentEditArtifact:
    """turn에서 미리 만든 문서 수정안. 결정적 계획의 입력이다."""

    id: str
    content_hash: str
    document_id: str
    base_version: int
    target: dict[str, object]
    summary: str
    expired: bool


@dataclass(frozen=True)
class ContentArtifactReference:
    id: str
    content_hash: str
    purpose: Literal["create_document", "apply_document_edit"]
    document_id: str | None = None
    base_version: int | None = None
    target: dict[str, object] | None = None
