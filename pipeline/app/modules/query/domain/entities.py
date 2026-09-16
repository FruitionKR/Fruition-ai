from dataclasses import dataclass, field
from typing import Literal


OutputLanguage = Literal["ko", "en", "document"]
ResponseLength = Literal["concise", "balanced", "detailed"]
ConversationRole = Literal["user", "assistant"]


@dataclass(frozen=True)
class WikiPage:
    id: str
    page_type: str
    title: str
    slug: str
    summary: str
    markdown_uri: str | None = None
    markdown: str | None = None
    source_document_id: str | None = None

    @property
    def is_source(self) -> bool:
        return self.page_type == "source"

    @property
    def is_concept(self) -> bool:
        return self.page_type == "concept"


@dataclass(frozen=True)
class WikiPageLink:
    from_page_id: str
    to_page_id: str
    link_type: str
    label: str | None = None
    confidence: float = 1.0


@dataclass(frozen=True)
class WikiEmbeddingUnit:
    id: str
    page_id: str
    source_document_id: str
    unit_type: str
    source_block_ids: list[str]
    text: str
    weight: float = 1.0


@dataclass(frozen=True)
class SemanticQueryEmbedding:
    model_name: str
    vector: list[float]


@dataclass(frozen=True)
class QueryRewrite:
    original_question: str
    retrieval_query: str
    keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConversationAgentRoute:
    action: str
    retrieval_source: Literal["none", "workspace", "web"]
    document_operation: Literal["none", "create", "edit"]
    persist: bool
    edit_goal: str | None = None
    edit_operation: Literal["replace", "insert_after"] | None = None
    edit_destination: Literal["target", "document_end"] | None = None
    selected_skill_id: str | None = None


@dataclass(frozen=True)
class ConversationMessage:
    role: ConversationRole
    content: str
    action: str | None = None
    run_id: str | None = None
    agent_route: ConversationAgentRoute | None = None


@dataclass(frozen=True)
class ConversationContext:
    recent_conversation_summary: str | None = None
    recent_messages: tuple[ConversationMessage, ...] = ()
    reference_context: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str
    content: str | None = None
    score: float = 1.0


@dataclass
class WebSearchTelemetry:
    requested: bool = False
    executed: bool = False
    result_count: int = 0
    error_code: str | None = None


@dataclass(frozen=True)
class RetrievedPage:
    page: WikiPage
    score: float
    role: str
    depth: int = 0


@dataclass(frozen=True)
class TraversalEdge:
    from_page_id: str
    to_page_id: str
    link_type: str
    role: str
    score: float


@dataclass(frozen=True)
class TraversalPath:
    path_id: str
    role: str
    nodes: list[str]
    edges: list[TraversalEdge]
    score: float
    used_for_answer: bool = True
    stop_reason: str = "answer_context_selected"


@dataclass(frozen=True)
class GraphContext:
    nodes: list[RetrievedPage] = field(default_factory=list)
    edges: list[TraversalEdge] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievalSummary:
    source_candidate_count: int
    concept_candidate_count: int
    visited_node_count: int
    returned_node_count: int
    used_source_count: int
    used_concept_count: int
    max_depth: int
    stop_reason: str


@dataclass(frozen=True)
class SourceReference:
    source_document_id: str
    source_block_id: str


@dataclass(frozen=True)
class EvidenceSnippet:
    rank: int
    source_document_id: str
    source_block_ids: list[str]
    text: str
    source_refs: list[SourceReference] = field(default_factory=list)


@dataclass(frozen=True)
class QueryContext:
    question: str
    graph_context: GraphContext
    traversal_paths: list[TraversalPath]
    related_pages: list[RetrievedPage]
    evidence_snippets: list[EvidenceSnippet]
    answer_context: str
    workspace_id: str | None = None
    user_id: str | None = None
    output_language: OutputLanguage | None = None
    response_length: ResponseLength | None = None
    allow_web_search: bool | None = None


@dataclass(frozen=True)
class GeneratedAnswer:
    content: str


@dataclass(frozen=True)
class QueryEvaluation:
    route: str
    evidence_relevance: float = 0.0
    citation_evidence_alignment: float | None = None
    unsupported_refusal_accuracy: float | None = None
    reason: str = ""
    feedback: str = ""
    web_query: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QueryAnswer:
    answer: GeneratedAnswer
    related_pages: list[RetrievedPage]
    evidence_snippets: list[EvidenceSnippet]
    graph_context: GraphContext
    traversal_paths: list[TraversalPath]
    retrieval_summary: RetrievalSummary
    updated_conversation_summary: str | None = None
    web_search: WebSearchTelemetry = field(default_factory=WebSearchTelemetry)
    evaluation: QueryEvaluation | None = None


@dataclass(frozen=True)
class QueryRetrieval:
    evidence_snippets: list[EvidenceSnippet]
    retrieval_summary: RetrievalSummary
