from typing import Any, Literal

from pydantic import BaseModel, Field, StrictBool, model_validator

from app.core.llm_env import resolve_llm_selection
from app.modules.query.domain.entities import ConversationMessage


class ConversationMessageRequest(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)
    action: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=128)

    def to_domain(self) -> ConversationMessage:
        return ConversationMessage(
            role=self.role,
            content=self.content,
            action=self.action,
            run_id=self.run_id,
        )


class QueryRequest(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    user_id: str | None = Field(default=None, min_length=1)
    question: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    allow_web_search: StrictBool
    recent_conversation_summary: str | None = None
    recent_messages: list[ConversationMessageRequest] = Field(default_factory=list, max_length=6)
    reference_context: dict[str, Any] | None = None
    output_language: Literal["ko", "en", "document"] | None = None
    response_length: Literal["concise", "balanced", "detailed"] | None = None

    @model_validator(mode="after")
    def validate_model_selection(self) -> "QueryRequest":
        resolve_llm_selection(self.provider, self.model)
        return self


class RelatedPageResponse(BaseModel):
    id: str
    page_type: str
    title: str
    slug: str
    relevance_score: float
    role: str
    depth: int


class TraversalEdgeResponse(BaseModel):
    from_page_id: str
    to_page_id: str
    link_type: str
    role: str
    score: float


class GraphContextResponse(BaseModel):
    nodes: list[RelatedPageResponse]
    edges: list[TraversalEdgeResponse]


class TraversalPathResponse(BaseModel):
    path_id: str
    role: str
    used_for_answer: bool
    score: float
    stop_reason: str
    nodes: list[str]
    edges: list[TraversalEdgeResponse]


class SourceReferenceResponse(BaseModel):
    source_document_id: str
    source_block_id: str


class EvidenceSnippetResponse(BaseModel):
    rank: int
    source_document_id: str
    source_block_ids: list[str]
    source_refs: list[SourceReferenceResponse]
    text: str


class QueryResponse(BaseModel):
    answer: str
    updated_conversation_summary: str | None = None
    related_pages: list[RelatedPageResponse]
    evidence_snippets: list[EvidenceSnippetResponse]
    graph_context: GraphContextResponse
    traversal_paths: list[TraversalPathResponse]
    web_search_requested: bool
    web_search_executed: bool
    result_count: int
    error_code: str | None = None
