from datetime import datetime
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.modules.agent_run.domain.entities import (
    AgentRun,
    McpApprovalCommand,
    McpProposalActor,
    McpProposalCommand,
)
from app.modules.agent_run.domain.plan import AgentPlan, AgentPlanOperation, ApprovedBy
from app.modules.skill.interfaces.http.schemas import McpSkillDefinition


class AgentRunActorRequest(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)


class _McpExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


_DOCUMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,254}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_HEX_HASH = re.compile(r"[0-9a-f]{64}")
_MAX_SIGNED_LONG = 2**63 - 1


def _bounded_nonblank(value: str, field: str, limit: int = 255) -> str:
    if not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be nonblank and at most {limit} characters")
    return value


class McpProposalActorRequest(_McpExactModel):
    invocation_id: str
    grant_id: str
    user_id: str
    workspace_id: str
    client_id: str
    policy_generation: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    origin: Literal["MCP_CLIENT"]
    spec_version: Literal["2025-11-25"]
    tool_name: Literal["propose_document_edit", "propose_plan", "propose_skill_definition"]

    @field_validator("invocation_id", "grant_id")
    @classmethod
    def canonical_uuid4(cls, value: str) -> str:
        parsed = UUID(value)
        if str(parsed) != value or parsed.version != 4:
            raise ValueError("must be a canonical UUIDv4")
        return value

    @field_validator("user_id", "workspace_id")
    @classmethod
    def bounded_actor_id(cls, value: str) -> str:
        return _bounded_nonblank(value, "actor id")

    @field_validator("client_id")
    @classmethod
    def clean_https_url(cls, value: str) -> str:
        if len(value) > 2048 or value != value.strip() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError("client_id must be a bounded clean HTTPS URL")
        parsed = urlsplit(value)
        try:
            parsed.port
        except ValueError:
            raise ValueError("client_id must contain a valid authority") from None
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("client_id must be an absolute HTTPS URL without credentials or fragment")
        return value

    def to_domain(self) -> McpProposalActor:
        return McpProposalActor(**self.model_dump())


class McpEditTargetRequest(_McpExactModel):
    type: Literal["selection", "current_section", "whole_document"]
    start_line: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    end_line: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)

    @field_validator("end_line")
    @classmethod
    def ordered_lines(cls, value: int, info) -> int:
        start = info.data.get("start_line")
        if isinstance(start, int) and value < start:
            raise ValueError("end_line must not precede start_line")
        return value


class McpEditArgumentsRequest(_McpExactModel):
    document_id: str
    base_version: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    target: McpEditTargetRequest
    content_artifact_id: str
    content_hash: str

    @field_validator("document_id")
    @classmethod
    def document_id_grammar(cls, value: str) -> str:
        if not _DOCUMENT_ID.fullmatch(value):
            raise ValueError("document_id has an invalid format")
        return value

    @field_validator("content_artifact_id")
    @classmethod
    def artifact_id_bound(cls, value: str) -> str:
        return _bounded_nonblank(value, "content_artifact_id")

    @field_validator("content_hash")
    @classmethod
    def content_hash_format(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("content_hash must be a lowercase sha256 digest")
        return value


class McpEditOperationRequest(_McpExactModel):
    id: str
    sequence: Literal[1]
    tool_name: Literal["apply_document_edit"]
    target_type: Literal["document"]
    target_id: str
    base_version: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    source_parent_id: str | None
    destination_parent_id: str | None
    arguments: McpEditArgumentsRequest
    reason: str
    depends_on: list[str] = Field(min_length=0, max_length=0)

    @field_validator("id", "reason")
    @classmethod
    def bounded_text(cls, value: str, info) -> str:
        return _bounded_nonblank(value, info.field_name, 1000 if info.field_name == "reason" else 255)

    @field_validator("target_id")
    @classmethod
    def target_id_grammar(cls, value: str) -> str:
        if not _DOCUMENT_ID.fullmatch(value):
            raise ValueError("target_id has an invalid format")
        return value

    @field_validator("source_parent_id", "destination_parent_id")
    @classmethod
    def canonical_folder_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError("folder id must be a canonical UUID")
        return value


class McpEditArtifactRequest(_McpExactModel):
    id: str
    content_hash: str
    purpose: Literal["apply_document_edit"]
    document_id: str
    base_version: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    target: McpEditTargetRequest
    markdown: str

    @field_validator("id")
    @classmethod
    def artifact_id_bound(cls, value: str) -> str:
        return _bounded_nonblank(value, "artifact id")

    @field_validator("document_id")
    @classmethod
    def document_id_grammar(cls, value: str) -> str:
        if not _DOCUMENT_ID.fullmatch(value):
            raise ValueError("document_id has an invalid format")
        return value

    @field_validator("content_hash")
    @classmethod
    def content_hash_format(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("content_hash must be a lowercase sha256 digest")
        return value

    @field_validator("markdown")
    @classmethod
    def markdown_bound(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("markdown exceeds the request limit")
        return value


class McpEditProposalRequest(_McpExactModel):
    instruction: str
    request_summary: str
    target_folder_id: None
    operations: list[McpEditOperationRequest] = Field(min_length=1, max_length=1)
    artifacts: list[McpEditArtifactRequest] = Field(min_length=1, max_length=1)

    @field_validator("instruction")
    @classmethod
    def instruction_bound(cls, value: str) -> str:
        if not value.strip() or len(value.strip()) > 1000:
            raise ValueError("instruction must be nonblank and at most 1000 trimmed characters")
        return value

    @field_validator("request_summary")
    @classmethod
    def summary_bound(cls, value: str) -> str:
        return _bounded_nonblank(value, "request_summary", 1000)


_PLAN_TOOLS = {
    "create_folder", "rename_folder", "move_folder", "move_document",
    "rename_document", "create_document", "apply_document_edit",
}
_PLAN_ARGUMENT_KEYS = {
    "create_folder": {"name", "parent_folder_id"},
    "rename_folder": {"folder_id", "name", "base_version"},
    "move_folder": {"folder_id", "parent_folder_id", "position", "base_version"},
    "move_document": {"document_id", "folder_id", "position", "base_version"},
    "rename_document": {"document_id", "display_name", "base_version"},
    "create_document": {"display_name", "folder_id", "content_artifact_id", "content_hash"},
    "apply_document_edit": {
        "document_id", "base_version", "target", "content_artifact_id", "content_hash",
    },
}


def _folder_id(value: object, *, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError("folder id must be a canonical UUID or null")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("folder id must be a canonical UUID")
    return value


def _result_ref(value: object, field: str) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"$operation_result", "field"}
        and isinstance(value.get("$operation_result"), str)
        and value.get("field") == field
    )


class McpPlanOperationRequest(_McpExactModel):
    id: str
    sequence: StrictInt = Field(ge=1, le=20)
    tool_name: Literal[
        "create_folder", "rename_folder", "move_folder", "move_document",
        "rename_document", "create_document", "apply_document_edit",
    ]
    target_type: Literal["folder", "document"]
    target_id: str | None
    base_version: StrictInt | None = Field(default=None, ge=1, le=_MAX_SIGNED_LONG)
    source_parent_id: str | None
    destination_parent_id: str | None
    arguments: dict[str, object]
    reason: str
    depends_on: list[str] = Field(max_length=19)

    @field_validator("id", "reason")
    @classmethod
    def bounded_text(cls, value: str, info) -> str:
        return _bounded_nonblank(value, info.field_name, 1000 if info.field_name == "reason" else 255)

    @field_validator("source_parent_id", "destination_parent_id")
    @classmethod
    def folder_ids(cls, value: str | None) -> str | None:
        return _folder_id(value)

    @model_validator(mode="after")
    def exact_tool_contract(self):
        if set(self.arguments) != _PLAN_ARGUMENT_KEYS[self.tool_name]:
            raise ValueError("operation arguments do not match the tool contract")
        folder_tool = self.tool_name in {"create_folder", "rename_folder", "move_folder"}
        if self.target_type != ("folder" if folder_tool else "document"):
            raise ValueError("target_type does not match tool_name")
        created = self.tool_name in {"create_folder", "create_document"}
        if created != (self.target_id is None and self.base_version is None):
            raise ValueError("create target/base binding is invalid")
        if not created:
            if not isinstance(self.target_id, str):
                raise ValueError("existing operation target_id is required")
            if self.target_type == "folder":
                _folder_id(self.target_id, nullable=False)
            elif not _DOCUMENT_ID.fullmatch(self.target_id):
                raise ValueError("document target_id has an invalid format")
        id_key = "folder_id" if folder_tool else "document_id"
        if not created and self.arguments[id_key] != self.target_id:
            raise ValueError("operation target does not match arguments")
        if not created:
            version = self.arguments.get("base_version")
            if version != self.base_version and not _result_ref(version, "current_version"):
                raise ValueError("operation version does not match arguments")
        destination_key = {
            "create_folder": "parent_folder_id", "create_document": "folder_id",
            "move_folder": "parent_folder_id", "move_document": "folder_id",
        }.get(self.tool_name)
        if destination_key:
            destination = self.arguments[destination_key]
            if _result_ref(destination, "id"):
                if self.destination_parent_id is not None:
                    raise ValueError("result destination requires null destination_parent_id")
            else:
                if _folder_id(destination) != self.destination_parent_id:
                    raise ValueError("destination does not match destination_parent_id")
        for key in ("name", "display_name"):
            if key in self.arguments:
                value = self.arguments[key]
                if not isinstance(value, str):
                    raise ValueError(f"{key} must be text")
                _bounded_nonblank(value, key)
        if "position" in self.arguments:
            position = self.arguments["position"]
            if position is not None and (type(position) is not int or not 0 <= position <= _MAX_SIGNED_LONG):
                raise ValueError("position must be null or a nonnegative integer")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("depends_on must not contain duplicates")
        return self


class McpPlanArtifactRequest(_McpExactModel):
    id: str
    content_hash: str
    purpose: Literal["create_document", "apply_document_edit"]
    document_id: str | None
    base_version: StrictInt | None = Field(default=None, ge=1, le=_MAX_SIGNED_LONG)
    target: McpEditTargetRequest | None
    markdown: str

    @field_validator("id")
    @classmethod
    def artifact_id_bound(cls, value: str) -> str:
        return _bounded_nonblank(value, "artifact id")

    @field_validator("content_hash")
    @classmethod
    def content_hash_format(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("content_hash must be a lowercase sha256 digest")
        return value

    @field_validator("markdown")
    @classmethod
    def markdown_bound(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("markdown exceeds the request limit")
        return value

    @model_validator(mode="after")
    def purpose_binding(self):
        if self.purpose == "create_document":
            if self.document_id is not None or self.base_version is not None or self.target is not None:
                raise ValueError("create_document artifact target fields must be null")
        elif (
            self.document_id is None or not _DOCUMENT_ID.fullmatch(self.document_id)
            or self.base_version is None or self.target is None
        ):
            raise ValueError("apply_document_edit artifact target fields are required")
        return self


class McpPlanProposalRequest(_McpExactModel):
    instruction: None
    request_summary: str
    target_folder_id: str | None
    operations: list[McpPlanOperationRequest] = Field(min_length=1, max_length=20)
    artifacts: list[McpPlanArtifactRequest] = Field(max_length=20)

    @field_validator("request_summary")
    @classmethod
    def summary_bound(cls, value: str) -> str:
        trimmed = value.strip()
        if value != trimmed:
            raise ValueError("request_summary must already be trimmed")
        return _bounded_nonblank(value, "request_summary", 1000)

    @field_validator("target_folder_id")
    @classmethod
    def target_folder(cls, value: str | None) -> str | None:
        return _folder_id(value)

    @model_validator(mode="after")
    def exact_graph(self):
        operation_ids = [operation.id for operation in self.operations]
        artifact_ids = [artifact.id for artifact in self.artifacts]
        if len(set(operation_ids)) != len(operation_ids) or len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("operation and artifact ids must be unique")
        if [operation.sequence for operation in self.operations] != list(range(1, len(self.operations) + 1)):
            raise ValueError("operation sequence must be contiguous")
        earlier: list[str] = []
        references: list[str] = []
        for operation in self.operations:
            expected = [item for item in earlier if item in operation.depends_on]
            if operation.depends_on != expected:
                raise ValueError("depends_on must contain earlier operations in plan order")
            earlier.append(operation.id)
            artifact_id = operation.arguments.get("content_artifact_id")
            if isinstance(artifact_id, str):
                references.append(artifact_id)
        if sorted(references) != sorted(artifact_ids) or len(references) != len(set(references)):
            raise ValueError("each artifact must be referenced by exactly one mutation operation")
        return self


class McpSkillProposalRequest(_McpExactModel):
    """외부 AI가 작성한 Skill 정의 하나를 웹 승인 대상으로 올린다."""

    request_summary: str
    definition: McpSkillDefinition

    @field_validator("request_summary")
    @classmethod
    def summary_bound(cls, value: str) -> str:
        return _bounded_nonblank(value, "request_summary", 1000)


class McpAgentProposalRequest(_McpExactModel):
    schema_version: Literal["mcp.agent-proposal.v1"]
    actor: McpProposalActorRequest
    idempotency_key: str
    request_hash: str
    proposal: McpEditProposalRequest | McpPlanProposalRequest | McpSkillProposalRequest

    @field_validator("idempotency_key")
    @classmethod
    def idempotency_bound(cls, value: str) -> str:
        return _bounded_nonblank(value, "idempotency_key")

    @field_validator("request_hash")
    @classmethod
    def request_hash_format(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("request_hash must be a lowercase sha256 digest")
        return value

    @model_validator(mode="after")
    def matching_tool(self):
        expected = {"propose_document_edit": McpEditProposalRequest, "propose_plan": McpPlanProposalRequest,
                    "propose_skill_definition": McpSkillProposalRequest}[self.actor.tool_name]
        if type(self.proposal) is not expected:
            raise ValueError("actor tool_name does not match proposal shape")
        return self

    def to_application(self) -> McpProposalCommand:
        return McpProposalCommand(
            actor=self.actor.to_domain(),
            idempotency_key=self.idempotency_key,
            request_hash=self.request_hash,
            proposal=self.proposal.model_dump(mode="python"),
        )


class McpAgentProposalResponse(_McpExactModel):
    schema_version: Literal["mcp.agent-proposal.v1"] = "mcp.agent-proposal.v1"
    run_id: str
    plan_id: str
    plan_version: StrictInt
    operation_hash: str
    status: Literal["awaiting_approval"]


class McpAgentApprovalRequest(_McpExactModel):
    schema_version: Literal["mcp.agent-approval.v1"]
    workspace_id: str
    user_id: str
    plan_version: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    operation_hash: str
    operation_ids: list[str] = Field(min_length=1, max_length=20)
    approval_id: str
    approval_mode: Literal["EXPLICIT_WEB"]
    grant_id: str
    client_id: str
    policy_generation: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)

    @field_validator("workspace_id", "user_id")
    @classmethod
    def bounded_actor_id(cls, value: str) -> str:
        return _bounded_nonblank(value, "actor id")

    @field_validator("operation_hash")
    @classmethod
    def operation_hash_format(cls, value: str) -> str:
        if not _HEX_HASH.fullmatch(value):
            raise ValueError("operation_hash must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("operation_ids")
    @classmethod
    def operation_ids_bound(cls, value: list[str]) -> list[str]:
        return [_bounded_nonblank(item, "operation_id") for item in value]

    @field_validator("approval_id", "grant_id")
    @classmethod
    def canonical_uuid4(cls, value: str) -> str:
        parsed = UUID(value)
        if str(parsed) != value or parsed.version != 4:
            raise ValueError("must be a canonical UUIDv4")
        return value

    @field_validator("client_id")
    @classmethod
    def clean_https_url(cls, value: str) -> str:
        return McpProposalActorRequest.clean_https_url(value)

    def to_application(self, run_id: str) -> McpApprovalCommand:
        return McpApprovalCommand(
            run_id=run_id,
            operation_ids=tuple(self.operation_ids),
            **self.model_dump(exclude={"operation_ids"}),
        )


class McpOwnedRunItemResponse(_McpExactModel):
    run_id: str
    status: str


class McpOwnedRunsResponse(_McpExactModel):
    items: list[McpOwnedRunItemResponse]
    next_cursor: str | None


class MarkdownAgentRunStatusResponse(BaseModel):
    id: str
    # 문서를 열지 않은 턴은 편집 대상이 없어 셋 다 비어 온다. 셋은 함께 있거나 함께 없다.
    document_id: str | None = None
    base_version: int | None = None
    apply_operation_id: str | None = None
    status: str
    result: dict[str, object] | None
    error_code: str | None


class ApproveAgentPlanRequest(AgentRunActorRequest):
    @model_validator(mode="before")
    @classmethod
    def do_not_accept_invalid_mcp_as_legacy_web(cls, value):
        if isinstance(value, dict) and "schema_version" in value:
            raise ValueError("MCP approval requests must satisfy the exact MCP schema")
        return value

    plan_version: int = Field(..., ge=1)
    operation_hash: str = Field(..., min_length=64, max_length=64)
    approved_by: ApprovedBy = Field(
        default="user",
        description=(
            "승인 주체. 사용자가 승인 화면에서 직접 승인하면 user(기본값), 사용자의 자동 승인 설정에 따라 "
            "document-svc가 대신 승인하면 auto:user-setting이다. 승인 기록(agent_approvals.approved_by)에 남는다."
        ),
    )
    # 빈 배열·20개 초과도 계약대로 400으로 거절하도록 길이 검사는 pydantic(422)이 아니라 도메인에서 한다.
    operation_ids: list[str] | None = Field(
        default=None,
        description=(
            "실행할 작업(plan.operations[].id) 1~20개. 생략하면 계획 전체를 실행한다. 고르지 않은 작업은 "
            "skipped(error_code=not_selected)가 된다. 빈 배열·20개 초과·중복·계획에 없는 ID·의존 작업 누락은 400으로 거절한다."
        ),
    )


class ReviseAgentPlanRequest(AgentRunActorRequest):
    instruction: str = Field(..., min_length=1, max_length=1000)


class AgentToolReadAuthorizationRequest(AgentRunActorRequest):
    run_id: str = Field(..., min_length=1)


class AgentToolExecuteAuthorizationRequest(AgentToolReadAuthorizationRequest):
    plan_id: str = Field(..., min_length=1)
    plan_version: int = Field(..., ge=1)
    operation_hash: str = Field(..., min_length=64, max_length=64)
    operation_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    arguments: dict[str, object]
    approval_id: str | None = None
    attempt_id: str | None = None
    grant_id: str | None = None
    client_id: str | None = None
    policy_generation: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_mcp_identity(self):
        values = (
            self.approval_id, self.attempt_id, self.grant_id, self.client_id, self.policy_generation,
        )
        if any(value is not None for value in values):
            if any(value is None for value in values):
                raise ValueError("MCP execution identity must be complete")
            for value in (self.approval_id, self.attempt_id, self.grant_id):
                parsed = UUID(value)
                if str(parsed) != value or parsed.version != 4:
                    raise ValueError("MCP execution UUIDs must be canonical UUIDv4")
            McpProposalActorRequest.clean_https_url(self.client_id)
        return self


class AgentArtifactRequest(BaseModel):
    run_id: str = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)


class AgentArtifactRegisterRequest(AgentArtifactRequest):
    artifact_id: str = Field(..., min_length=1)
    content_hash: str = Field(..., min_length=71, max_length=71)
    purpose: str = Field(..., min_length=1)
    document_id: str | None = None
    base_version: int | None = Field(default=None, ge=1)
    target: dict[str, object] | None = None
    markdown: str = Field(..., min_length=0, max_length=5_242_880)


class AgentArtifactResolveRequest(AgentArtifactRequest):
    artifact_id: str = Field(..., min_length=1)
    content_hash: str = Field(..., min_length=71, max_length=71)
    purpose: str = Field(..., min_length=1)
    document_id: str | None = None
    base_version: int | None = Field(default=None, ge=1)
    target: dict[str, object] | None = None


class AgentArtifactListRequest(AgentArtifactRequest):
    pass


class AgentArtifactResponse(BaseModel):
    id: str
    content_hash: str
    purpose: str
    document_id: str | None = None
    base_version: int | None = None
    target: dict[str, object] | None = None


class AgentArtifactResolveResponse(AgentArtifactResponse):
    markdown: str


class MarkdownDiffLineResponse(BaseModel):
    type: Literal["context", "delete", "insert"]
    text: str


class AgentDocumentEditHunkResponse(BaseModel):
    before_start: int
    after_start: int
    lines: list[MarkdownDiffLineResponse]


class AgentDocumentEditPreviewResponse(BaseModel):
    operation_id: str
    tool_name: Literal["apply_document_edit", "create_document"] = "apply_document_edit"
    document_id: str | None
    display_name: str
    base_version: int | None
    summary: str
    added_lines: int
    removed_lines: int
    hunks: list[AgentDocumentEditHunkResponse]

    @model_validator(mode="after")
    def validate_identity(self) -> "AgentDocumentEditPreviewResponse":
        if self.tool_name == "create_document":
            if self.document_id is not None or self.base_version is not None or self.removed_lines != 0:
                raise ValueError("create_document preview must not invent an existing document identity")
        elif self.document_id is None or self.base_version is None or self.base_version < 1:
            raise ValueError("apply_document_edit preview requires a positive document base version")
        return self


class AgentRunEditPreviewsResponse(BaseModel):
    items: list[AgentDocumentEditPreviewResponse]
    expires_at: datetime | None = None


class AgentPlanOperationWarningResponse(BaseModel):
    type: str = Field(..., description="duplicate_name(같은 위치에 같은 이름), duplicate_content(본문이 거의 같은 문서) 또는 depends_on_unselected")
    message: str


class AgentOperationExistingResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: Literal["DOCUMENT", "FOLDER"]
    resource_id: str
    current_version: int = Field(..., gt=0)


class AgentPlanOperationResponse(BaseModel):
    id: str
    sequence: int
    tool_name: str
    target_type: str
    target_id: str | None
    base_version: int | None
    source_parent_id: str | None
    destination_parent_id: str | None
    arguments: dict[str, object]
    reason: str
    depends_on: list[str]
    status: str
    error_code: str | None
    # 중복 의심 등 승인 전에 확인할 점. 없으면 null이다.
    warning: AgentPlanOperationWarningResponse | None = None
    # 승인 화면의 기본 선택 여부. 중복이 의심되거나 그런 작업에 의존하면 false다.
    suggested_selected: bool = True
    existing_result: AgentOperationExistingResultResponse | None = None

    @classmethod
    def from_domain(cls, operation: AgentPlanOperation) -> "AgentPlanOperationResponse":
        return cls(
            id=operation.id,
            sequence=operation.sequence,
            tool_name=operation.tool_name,
            target_type=operation.target_type,
            target_id=operation.target_id,
            base_version=operation.base_version,
            source_parent_id=operation.source_parent_id,
            destination_parent_id=operation.destination_parent_id,
            arguments=operation.arguments,
            reason=operation.reason,
            depends_on=list(operation.depends_on),
            status=operation.status,
            error_code=operation.error_code,
            warning=(
                AgentPlanOperationWarningResponse.model_validate(operation.warning)
                if operation.warning is not None
                else None
            ),
            suggested_selected=operation.suggested_selected,
            existing_result=(
                AgentOperationExistingResultResponse.model_validate(operation.existing_result)
                if operation.existing_result is not None
                else None
            ),
        )


class AgentPlanResponse(BaseModel):
    id: str
    version: int
    summary: str
    operation_hash: str
    status: str
    operations: list[AgentPlanOperationResponse]
    # 승인 주체. "user"(승인 화면에서 직접) 또는 "auto:user-setting"(사용자의 자동 승인 설정). 승인 전이면 null이다.
    approved_by: str | None = None

    @classmethod
    def from_domain(cls, plan: AgentPlan) -> "AgentPlanResponse":
        return cls(
            id=plan.id,
            version=plan.version,
            summary=plan.summary,
            operation_hash=plan.operation_hash,
            status=plan.status,
            approved_by=plan.approved_by,
            operations=[AgentPlanOperationResponse.from_domain(operation) for operation in plan.operations],
        )


class AgentRunResponse(BaseModel):
    id: str
    workspace_id: str
    action: str
    skill_version_id: str | None
    status: str
    request_summary: str
    error_code: str | None
    # 계획 단계가 의도를 확정하지 못해 clarification_required가 된 run에서만 채워진다.
    clarification_question: str | None = None
    # 턴에서 지정한 작업 대상 폴더(폴더 정리 범위·새 문서 위치). 지정하지 않았으면 null이다.
    target_folder_id: str | None = None
    # 선택한 Skill의 규칙 요약(실행 시점 Skill 설명). Skill 없이 실행했으면 null이다.
    rule_summary: str | None = None
    origin: str | None = None
    grant_id: str | None = None
    client_id: str | None = None
    policy_generation: int | None = None
    invocation_id: str | None = None
    spec_version: str | None = None
    tool_name: str | None = None
    plan: AgentPlanResponse | None = None

    @classmethod
    def from_domain(cls, run: AgentRun, plan: AgentPlan | None = None) -> "AgentRunResponse":
        return cls(
            id=run.id,
            workspace_id=run.workspace_id,
            action=run.action,
            skill_version_id=run.skill_version_id,
            status=run.status,
            request_summary=run.request_summary,
            error_code=run.error_code,
            clarification_question=(
                run.clarification_question if run.status == "clarification_required" else None
            ),
            target_folder_id=run.target_folder_id,
            rule_summary=run.rule_summary,
            origin=run.origin,
            grant_id=run.grant_id,
            client_id=run.client_id,
            policy_generation=run.policy_generation,
            invocation_id=run.invocation_id,
            spec_version=run.spec_version,
            tool_name=run.tool_name,
            plan=AgentPlanResponse.from_domain(plan) if plan else None,
        )
