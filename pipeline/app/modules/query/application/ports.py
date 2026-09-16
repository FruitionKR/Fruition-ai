from typing import Protocol, runtime_checkable

from app.modules.query.domain.entities import (
    ConversationMessage,
    EvidenceSnippet,
    GeneratedAnswer,
    QueryContext,
    QueryEvaluation,
    QueryRewrite,
    SemanticQueryEmbedding,
    WebSearchResult,
    WikiEmbeddingUnit,
    WikiPage,
    WikiPageLink,
)


class WikiRepositoryPort(Protocol):
    def list_candidate_pages(
        self,
        workspace_id: str,
        query: str,
        source_limit: int,
        concept_limit: int,
        semantic_query: SemanticQueryEmbedding | None = None,
    ) -> list[WikiPage]:
        ...

    def list_links_for_page_ids(
        self,
        workspace_id: str,
        page_ids: list[str],
        limit: int,
        excluded_page_ids: list[str] | None = None,
    ) -> list[WikiPageLink]:
        ...

    def list_pages_by_ids(
        self,
        workspace_id: str,
        page_ids: list[str],
    ) -> list[WikiPage]:
        ...

    def list_embedding_units_by_page_ids(self, page_ids: list[str]) -> dict[str, list[WikiEmbeddingUnit]]:
        ...


class WikiMarkdownReaderPort(Protocol):
    def read_markdown(self, markdown_uri: str) -> str:
        ...


class EmbeddingSearchPort(Protocol):
    def score(self, query: str, documents: list[str]) -> list[float]:
        ...


@runtime_checkable
class SemanticQueryEmbeddingPort(Protocol):
    def embed_query(self, query: str) -> SemanticQueryEmbedding:
        ...


class TextSearchPort(Protocol):
    def score(self, query: str, documents: list[str]) -> list[float]:
        ...


class QueryRewritePort(Protocol):
    def rewrite(self, question: str) -> QueryRewrite:
        ...


class ConversationSummarizerPort(Protocol):
    def summarize(
        self,
        previous_summary: str | None,
        messages: tuple[ConversationMessage, ...],
    ) -> str:
        ...


class WebSearchPort(Protocol):
    def search(self, query: str) -> list[WebSearchResult]:
        ...


class AnswerGeneratorPort(Protocol):
    def generate_answer(self, context: QueryContext) -> GeneratedAnswer:
        ...


class QueryEvaluatorPort(Protocol):
    def evaluate(
        self,
        question: str,
        context: QueryContext,
        answer: GeneratedAnswer,
        stop_reason: str,
        web_search_available: bool = False,
    ) -> QueryEvaluation:
        ...


class QueryEvaluatorGraphPort(Protocol):
    def run(
        self,
        *,
        question: str,
        query_context: QueryContext,
        stop_reason: str,
        event_publisher: "QueryEventPublisherPort | None",
    ) -> tuple[GeneratedAnswer, list[EvidenceSnippet], QueryContext, QueryEvaluation | None]:
        ...


class QueryEventPublisherPort(Protocol):
    def publish(self, stage: str, message: str, data: dict[str, object] | None = None) -> None:
        ...
