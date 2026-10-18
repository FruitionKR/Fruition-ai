import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.llm_env import resolve_workspace_llm_selection
from app.modules.skill.domain.entities import (
    Skill,
    SkillAuthoringResult,
    SkillDraftSourceOperation,
    SkillDraftSourceRun,
    SkillVersion,
)


CapabilityValue = Literal["document-create", "document-edit", "folder-organize", "template"]
TargetValue = Literal["current_document", "selection"]
ResultModeValue = Literal["create", "edit", "auto"]
ToolValue = Literal[
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


class SkillLlmRequest(BaseModel):
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_model_selection(self) -> "SkillLlmRequest":
        # 워크스페이스 공용 모델 설정에서 온 값이다. 허용 범위 밖이면 워크스페이스 기본 모델로 대체한다.
        self.provider, self.model = resolve_workspace_llm_selection(self.provider, self.model)
        return self


class SkillDefinitionRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    description: str = Field(..., min_length=1)
    instructions_markdown: str = Field(..., min_length=1)
    capabilities: list[CapabilityValue] = Field(..., min_length=1)
    allowed_tools: list[ToolValue] = Field(default_factory=list)


class SkillAuthoringRequest(SkillLlmRequest):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    scope_type: Literal["personal", "team"]
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9-]{0,62}$",
    )
    description: str | None = Field(default=None, min_length=1, max_length=500)
    instruction: str = Field(..., min_length=1, max_length=30_000)
    authoring_mode: Literal["preserve", "enhance", "regenerate"] = "enhance"
    reference_document_ids: list[str] = Field(default_factory=list, max_length=3)


class SkillAuthoringResponse(BaseModel):
    status: Literal["clarification_required", "blocked", "proposal_ready", "published"]
    question: str | None = None
    skill_id: str | None = None
    version_id: str | None = None
    scope_type: Literal["personal", "team"] | None = None
    name: str | None = None
    description: str | None = None
    skill_markdown: str | None = None
    instructions_markdown: str | None = None
    capabilities: list[CapabilityValue] = Field(default_factory=list)
    allowed_tools: list[ToolValue] = Field(default_factory=list)
    targets: list[TargetValue] = Field(default_factory=list)
    result_mode: ResultModeValue | None = None
    issues: list[dict[str, object]] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, result: SkillAuthoringResult) -> "SkillAuthoringResponse":
        if result.proposal is not None:
            proposal = result.proposal
            skill = result.skill
            version = skill.enabled_version if skill else None
            stored = version or (skill.latest_version if skill else None)
            return cls(
                status=result.status,
                skill_id=skill.id if skill else None,
                version_id=version.id if version else None,
                scope_type=proposal.scope_type,
                name=proposal.name,
                description=proposal.description,
                skill_markdown=_skill_markdown(
                    proposal.name,
                    proposal.description,
                    proposal.instructions_markdown,
                ),
                instructions_markdown=proposal.instructions_markdown,
                capabilities=list(proposal.capabilities),
                allowed_tools=list(proposal.allowed_tools),
                # 실행 대상·결과 방식은 게시된 버전에만 있다(초안 단계는 빈 값).
                targets=list(stored.targets) if stored else [],
                result_mode=stored.result_mode if stored else None,
                issues=[issue.__dict__ for issue in result.issues],
            )
        if result.skill is None:
            return cls(
                status=result.status,
                question=result.question,
                issues=[issue.__dict__ for issue in result.issues],
            )
        version = result.skill.enabled_version or result.skill.latest_version
        if version is None:
            raise ValueError("Authored Skill version is missing.")
        return cls(
            status=result.status,
            skill_id=result.skill.id,
            version_id=version.id,
            scope_type=result.skill.scope_type,
            name=version.name,
            description=version.description,
            skill_markdown=_skill_markdown(version.name, version.description, version.instructions_markdown),
            instructions_markdown=version.instructions_markdown,
            capabilities=list(version.capabilities),
            allowed_tools=list(version.allowed_tools),
            targets=list(version.targets),
            result_mode=version.result_mode,
        )


class PublishAuthoredSkillRequest(SkillLlmRequest):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    scope_type: Literal["personal", "team"]
    name: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    description: str = Field(..., min_length=1, max_length=500)
    instructions_markdown: str = Field(..., min_length=1, max_length=30_000)
    capabilities: list[CapabilityValue] = Field(..., min_length=1)
    # 생략하거나 비우면 서버가 capabilities로 계산한다(policy.canonical_allowed_tools).
    allowed_tools: list[ToolValue] = Field(default_factory=list)
    # 문서 Skill의 실행 대상·결과 방식. 생략하면 targets는 기본값(["current_document"]), result_mode는 null이다.
    targets: list[TargetValue] | None = Field(default=None, max_length=2)
    result_mode: ResultModeValue | None = None


class UpdateSkillRequest(SkillLlmRequest):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    description: str = Field(..., min_length=1, max_length=500)
    instructions_markdown: str = Field(..., min_length=1, max_length=30_000)
    # 생략(null)하면 기존 값을 유지한다. targets=[]는 기본값(["current_document"])으로 되돌린다.
    targets: list[TargetValue] | None = Field(default=None, max_length=2)
    result_mode: ResultModeValue | None = None


class SkillActorRequest(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)


class SkillVersionResponse(BaseModel):
    id: str
    version: int
    name: str
    description: str
    instructions_markdown: str
    capabilities: list[str]
    allowed_tools: list[str]
    targets: list[str]
    result_mode: str | None
    lint_result: dict[str, object]
    status: str

    @classmethod
    def from_domain(cls, version: SkillVersion) -> "SkillVersionResponse":
        return cls(
            id=version.id,
            version=version.version,
            name=version.name,
            description=version.description,
            instructions_markdown=version.instructions_markdown,
            capabilities=list(version.capabilities),
            allowed_tools=list(version.allowed_tools),
            targets=list(version.targets),
            result_mode=version.result_mode,
            lint_result=version.lint_result or {},
            status=version.status,
        )


class SkillResponse(BaseModel):
    id: str
    workspace_id: str | None
    scope_type: str
    owner_user_id: str | None
    slug: str
    status: str
    enabled_version: SkillVersionResponse | None
    latest_version: SkillVersionResponse | None

    @classmethod
    def from_domain(cls, skill: Skill) -> "SkillResponse":
        return cls(
            id=skill.id,
            workspace_id=skill.workspace_id,
            scope_type=skill.scope_type,
            owner_user_id=skill.owner_user_id,
            slug=skill.slug,
            status=skill.status,
            enabled_version=(SkillVersionResponse.from_domain(skill.enabled_version) if skill.enabled_version else None),
            latest_version=(SkillVersionResponse.from_domain(skill.latest_version) if skill.latest_version else None),
        )


class SkillPreviewResponse(BaseModel):
    lint_result: dict[str, object]
    has_blocked_issues: bool

    @classmethod
    def from_domain(cls, version: SkillVersion) -> "SkillPreviewResponse":
        issues = (version.lint_result or {}).get("issues", [])
        return cls(
            lint_result=version.lint_result or {},
            has_blocked_issues=any(
                isinstance(issue, dict) and issue.get("severity") == "blocked" for issue in issues
            ),
        )


class SkillDraftSourceOperationRequest(BaseModel):
    tool_name: ToolValue
    reason: str = Field(..., min_length=1)

    def to_domain(self) -> SkillDraftSourceOperation:
        return SkillDraftSourceOperation(tool_name=self.tool_name, reason=self.reason)


class SkillDraftSourceRunRequest(BaseModel):
    run_id: str = Field(..., min_length=1)
    status: Literal["completed"]
    request_summary: str = Field(..., min_length=1)
    plan_summary: str = Field(..., min_length=1)
    successful_operations: list[SkillDraftSourceOperationRequest] = Field(..., min_length=1)

    def to_domain(self) -> SkillDraftSourceRun:
        return SkillDraftSourceRun(
            run_id=self.run_id,
            status=self.status,
            request_summary=self.request_summary,
            plan_summary=self.plan_summary,
            successful_operations=tuple(operation.to_domain() for operation in self.successful_operations),
        )


class SkillDraftProposalRequest(SkillLlmRequest):
    workspace_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    scope_type: Literal["personal", "team"]
    source_runs: list[SkillDraftSourceRunRequest] = Field(..., min_length=1)
    user_directives: list[str] = Field(default_factory=list)
    excluded_literals: list[str] = Field(default_factory=list)

def _skill_markdown(name: str, description: str, instructions_markdown: str) -> str:
    return (
        "---\n"
        f"name: {json.dumps(name, ensure_ascii=False)}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "---\n\n"
        f"{instructions_markdown}"
    )


class SkillTaskRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=120)
    kind: Literal["skill_author", "skill_publish", "skill_update"]
    workspace_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    payload: dict[str, object]
    skill_id: str | None = None
