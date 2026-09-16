from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.modules.skill.domain.safety import SkillSafetyIssue


SkillCapability = Literal["document-create", "document-edit", "folder-organize", "template"]
SkillTool = Literal[
    "list_root_items",
    "list_folder_children",
    "search_hierarchy",
    "get_breadcrumb",
    "get_document_metadata",
    "get_document_content",
    "create_folder",
    "rename_folder",
    "move_folder",
    "move_document",
    "rename_document",
    "create_document",
    "apply_document_edit",
]
SkillScopeType = Literal["personal", "team"]
SkillAuthoringMode = Literal["preserve", "enhance", "regenerate"]
SkillStatus = Literal["enabled", "disabled"]
SkillVersionStatus = Literal["draft", "published", "rejected"]


@dataclass(frozen=True)
class SkillVersion:
    id: str
    skill_id: str
    version: int
    name: str
    description: str
    instructions_markdown: str
    capabilities: tuple[SkillCapability, ...]
    allowed_tools: tuple[SkillTool, ...] = ()
    status: SkillVersionStatus = "draft"
    lint_result: dict[str, object] | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    published_at: datetime | None = None


@dataclass(frozen=True)
class Skill:
    id: str
    workspace_id: str | None
    scope_type: SkillScopeType
    owner_user_id: str | None
    slug: str
    status: SkillStatus
    enabled_version: SkillVersion | None = None
    latest_version: SkillVersion | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class SkillDraftSourceOperation:
    tool_name: SkillTool
    reason: str


@dataclass(frozen=True)
class SkillDraftSourceRun:
    run_id: str
    status: str
    request_summary: str
    plan_summary: str
    successful_operations: tuple[SkillDraftSourceOperation, ...]


@dataclass(frozen=True)
class SkillDraftProposal:
    name: str
    description: str
    instructions_markdown: str
    capabilities: tuple[SkillCapability, ...]
    allowed_tools: tuple[SkillTool, ...]
    source_run_ids: tuple[str, ...]
    persisted: Literal[False] = False


@dataclass(frozen=True)
class SkillAuthoringReference:
    id: str
    name: str
    markdown: str


@dataclass(frozen=True)
class SkillAuthoringProposal:
    workspace_id: str
    user_id: str
    scope_type: SkillScopeType
    name: str
    description: str
    instructions_markdown: str
    capabilities: tuple[SkillCapability, ...] = ()
    allowed_tools: tuple[SkillTool, ...] = ()


@dataclass(frozen=True)
class SkillAuthoringResult:
    status: Literal["clarification_required", "blocked", "proposal_ready", "published"]
    question: str | None = None
    proposal: SkillAuthoringProposal | None = None
    skill: Skill | None = None
    issues: tuple[SkillSafetyIssue, ...] = ()
