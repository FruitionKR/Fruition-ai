from dataclasses import dataclass
from datetime import datetime
from typing import Literal


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
]


@dataclass(frozen=True)
class StartAgentRunContent:
    markdown: str
    purpose: Literal["create_document", "apply_document_edit"] = "create_document"
    document_id: str | None = None
    base_version: int | None = None
    target: dict[str, object] | None = None


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
    # 작업 대상 폴더. 폴더 정리 범위이자 새 문서를 둘 위치다. None이면 워크스페이스 전체·루트다.
    target_folder_id: str | None = None


@dataclass(frozen=True)
class StartAgentRunArtifact:
    id: str
    content_hash: str
    markdown: str
    purpose: Literal["create_document", "apply_document_edit"] = "create_document"
    document_id: str | None = None
    base_version: int | None = None
    target: dict[str, object] | None = None


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
class ContentArtifactReference:
    id: str
    content_hash: str
    purpose: Literal["create_document", "apply_document_edit"]
    document_id: str | None = None
    base_version: int | None = None
    target: dict[str, object] | None = None
