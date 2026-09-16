from typing import Any, Literal, Self

from pydantic import BaseModel, Field, StrictBool, model_validator

from app.core.untrusted_input import validate_untrusted_payload
from app.core.llm_env import resolve_llm_selection
from app.modules.agent.domain.entities import (
    ActiveMarkdownContext,
    AgentConversationContext,
    AgentTurnRequest,
    PendingSkillProposal,
)
from app.modules.markdown_edit.domain.entities import MarkdownEditTarget
from app.modules.query.domain.entities import ConversationAgentRoute, ConversationMessage
from app.modules.query.interfaces.http.schemas import ConversationMessageRequest, QueryResponse
from app.modules.skill.interfaces.http.schemas import (
    CapabilityValue,
    SkillAuthoringResponse,
    SkillDraftSourceRunRequest,
    ToolValue,
)


MAX_AGENT_MESSAGE_LENGTH = 1000


class MarkdownEditTargetRequest(BaseModel):
    type: Literal["selection", "current_section", "whole_document"]
    start_line: int = Field(..., ge=1)
    end_line: int = Field(..., ge=1)

    def to_domain(self) -> MarkdownEditTarget:
        return MarkdownEditTarget(
            type=self.type,
            start_line=self.start_line,
            end_line=self.end_line,
        )


class ActiveMarkdownContextRequest(BaseModel):
    markdown: str
    target: MarkdownEditTargetRequest | None = None

    def to_domain(self) -> ActiveMarkdownContext:
        return ActiveMarkdownContext(
            markdown=self.markdown,
            target=self.target.to_domain() if self.target else None,
        )


class ConversationAgentRouteRequest(BaseModel):
    action: str = Field(..., min_length=1, max_length=64)
    retrieval_source: Literal["none", "workspace", "web"]
    document_operation: Literal["none", "create", "edit"]
    persist: StrictBool
    edit_goal: str | None = Field(default=None, max_length=64)
    edit_operation: Literal["replace", "insert_after"] | None = None
    edit_destination: Literal["target", "document_end"] | None = None
    selected_skill_id: str | None = Field(default=None, max_length=128)

    def to_domain(self) -> ConversationAgentRoute:
        return ConversationAgentRoute(
            action=self.action,
            retrieval_source=self.retrieval_source,
            document_operation=self.document_operation,
            persist=self.persist,
            edit_goal=self.edit_goal,
            edit_operation=self.edit_operation,
            edit_destination=self.edit_destination,
            selected_skill_id=self.selected_skill_id,
        )


class AgentConversationMessageRequest(ConversationMessageRequest):
    agent_route: ConversationAgentRouteRequest | None = None

    def to_domain(self) -> ConversationMessage:
        return ConversationMessage(
            role=self.role,
            content=self.content,
            action=self.action,
            run_id=self.run_id,
            agent_route=self.agent_route.to_domain() if self.agent_route else None,
        )


class PendingSkillProposalRequest(BaseModel):
    scope_type: Literal["personal", "team"]
    name: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    description: str = Field(..., min_length=1, max_length=500)
    instructions_markdown: str = Field(..., min_length=1, max_length=30_000)
    capabilities: list[CapabilityValue] = Field(..., min_length=1)
    allowed_tools: list[ToolValue] = Field(..., min_length=1)

    def to_domain(self) -> PendingSkillProposal:
        return PendingSkillProposal(
            scope_type=self.scope_type,
            name=self.name,
            description=self.description,
            instructions_markdown=self.instructions_markdown,
            capabilities=tuple(self.capabilities),
            allowed_tools=tuple(self.allowed_tools),
        )


class AgentConversationContextRequest(BaseModel):
    recent_conversation_summary: str | None = None
    recent_messages: list[AgentConversationMessageRequest] = Field(default_factory=list, max_length=6)
    reference_context: dict[str, Any] | None = None
    pending_skill_proposal: PendingSkillProposalRequest | None = None

    def to_domain(self) -> AgentConversationContext:
        return AgentConversationContext(
            recent_conversation_summary=self.recent_conversation_summary,
            recent_messages=tuple(message.to_domain() for message in self.recent_messages),
            reference_context=self.reference_context or {},
            pending_skill_proposal=(
                self.pending_skill_proposal.to_domain() if self.pending_skill_proposal else None
            ),
        )


class AgentTurnRequestBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_AGENT_MESSAGE_LENGTH)
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    workspace_id: str | None = Field(default=None, min_length=1)
    user_id: str | None = Field(default=None, min_length=1)
    output_language: Literal["ko", "en", "document"] | None = None
    response_length: Literal["concise", "balanced", "detailed"] | None = None
    allow_web_search: bool | None = None
    conversation_context: AgentConversationContextRequest | None = None
    active_markdown_context: ActiveMarkdownContextRequest | None = None
    document_id: str | None = Field(default=None, min_length=1)
    base_version: int | None = Field(default=None, ge=0)
    skill_mode: Literal["auto", "explicit", "off"] = "auto"
    skill_id: str | None = Field(default=None, min_length=1)
    skill_draft_sources: list[SkillDraftSourceRunRequest] = Field(default_factory=list)
    skill_draft_user_directives: list[str] = Field(default_factory=list)
    skill_draft_excluded_literals: list[str] = Field(default_factory=list)
    skill_scope_type: Literal["personal", "team"] | None = None
    skill_authoring_mode: Literal["preserve", "enhance"] = "enhance"
    skill_reference_document_ids: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_skill_selection(self) -> Self:
        if self.skill_mode == "auto" and self.skill_id is not None:
            raise ValueError("skill_id must be omitted or null when skill_mode is auto")
        if self.skill_mode == "explicit" and (self.skill_id is None or not self.skill_id.strip()):
            raise ValueError("skill_id is required when skill_mode is explicit")
        if self.skill_mode == "off" and self.skill_id is not None:
            raise ValueError("skill_id must be omitted or null when skill_mode is off")
        return self

    @model_validator(mode="after")
    def validate_untrusted_input(self) -> Self:
        resolve_llm_selection(self.provider, self.model)
        validate_untrusted_payload(self.model_dump(mode="json"))
        return self

    def to_domain(self) -> AgentTurnRequest:
        return AgentTurnRequest(
            message=self.message,
            provider=self.provider,
            model=self.model,
            workspace_id=self.workspace_id,
            user_id=self.user_id,
            output_language=self.output_language,
            response_length=self.response_length,
            allow_web_search=self.allow_web_search,
            conversation_context=self.conversation_context.to_domain() if self.conversation_context else None,
            active_markdown_context=self.active_markdown_context.to_domain() if self.active_markdown_context else None,
            document_id=self.document_id,
            base_version=self.base_version,
            skill_mode=self.skill_mode,
            skill_id=self.skill_id,
            skill_draft_sources=tuple(source.to_domain() for source in self.skill_draft_sources),
            skill_draft_user_directives=tuple(self.skill_draft_user_directives),
            skill_draft_excluded_literals=tuple(self.skill_draft_excluded_literals),
            skill_scope_type=self.skill_scope_type,
            skill_authoring_mode=self.skill_authoring_mode,
            skill_reference_document_ids=tuple(self.skill_reference_document_ids),
        )


class AgentTurnRouteResponse(BaseModel):
    action: Literal[
        "chat_answer",
        "conversation_reply",
        "markdown_edit",
        "markdown_create",
        "folder_organize",
        "workspace_workflow",
        "skill_authoring",
        "skill_draft_proposal",
        "clarify",
        "reject",
    ]
    confidence: float
    reason: str
    edit_goal: str | None = None
    edit_operation: Literal["replace", "insert_after"] | None = None
    edit_destination: Literal["target", "document_end"] | None = None
    selected_skill_id: str | None = None
    skill_candidates: list[str] = Field(default_factory=list)
    retrieval_source: Literal["none", "workspace", "web"]
    document_operation: Literal["none", "create", "edit"]
    persist: bool
    required_capabilities: list[
        Literal["document-create", "document-edit", "folder-organize", "template"]
    ] = Field(default_factory=list)


class SkillCandidateResponse(BaseModel):
    id: str
    version_id: str
    name: str
    description: str
    capabilities: list[str]


class MarkdownEditTargetResponse(BaseModel):
    type: Literal["selection", "current_section", "whole_document"]
    start_line: int
    end_line: int


class MarkdownEditOperationResponse(BaseModel):
    operation: Literal["replace", "insert_after"]
    requested_target: MarkdownEditTargetResponse
    actual_target: MarkdownEditTargetResponse
    scope_expanded: bool
    changed: bool
    summary: str
    replacement_markdown: str


class GeneratedMarkdownResponse(BaseModel):
    title: str
    summary: str
    markdown: str


class AgentTurnResponse(BaseModel):
    action: Literal[
        "chat_answer",
        "conversation_reply",
        "markdown_edit",
        "markdown_create",
        "folder_organize",
        "workspace_workflow",
        "skill_authoring",
        "skill_draft_proposal",
        "clarify",
        "reject",
    ]
    route: AgentTurnRouteResponse
    updated_conversation_summary: str | None = None
    message: str | None = None
    chat: QueryResponse | None = None
    edit: MarkdownEditOperationResponse | None = None
    source_markdown_sha256: str | None = None
    generated_markdown: GeneratedMarkdownResponse | None = None
    skill_candidates: list[SkillCandidateResponse] = Field(default_factory=list)
    run_id: str | None = None
    run_status: str | None = None
    skill_authoring: SkillAuthoringResponse | None = None
