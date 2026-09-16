import os
from functools import lru_cache

from app.modules.query.application.answer_query import AnswerQueryUseCase
from app.modules.query.application.ports import (
    QueryEventPublisherPort,
    WikiRepositoryPort,
)
from app.modules.query.application.query_answer_assembler import QueryAnswerAssembler
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.query.infrastructure.minio_wiki_markdown_reader import MinioWikiMarkdownReader
from app.modules.query.infrastructure.postgres_wiki_repository import PostgresWikiRepository
from app.modules.query.infrastructure.query_chat_answer_generator import (
    build_query_chat_answer_generator,
    build_query_conversation_summarizer,
)
from app.modules.query.infrastructure.query_answer_evaluator import build_query_answer_evaluator
from app.modules.query.infrastructure.query_evaluator_graph import LangGraphQueryEvaluatorGraph
from app.modules.query.infrastructure.query_event_publisher import NoOpQueryEventPublisher
from app.modules.query.infrastructure.rule_based_query_rewriter import RuleBasedQueryRewriter
from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch
from app.modules.query.infrastructure.web_search import build_web_search
from app.modules.query.interfaces.http.schemas import QueryRequest


def build_answer_query_use_case(
    *,
    provider: str | None = None,
    model: str | None = None,
    allow_web_search: bool = False,
    event_publisher: QueryEventPublisherPort | None = None,
    require_evaluator: bool = False,
    evaluator_mode: str | None = None,
    wiki_repository: WikiRepositoryPort | None = None,
) -> AnswerQueryUseCase:
    text_search = Bm25Searcher()
    answer_generator = build_query_chat_answer_generator(provider=provider, model=model)
    query_answer_assembler = QueryAnswerAssembler(answer_generator)
    web_search = build_web_search(allow_web_search)
    query_evaluator = build_query_answer_evaluator(
        provider=provider,
        model=model,
        web_search_available=web_search is not None,
        mode="llm" if require_evaluator else evaluator_mode,
    )
    conversation_summarizer = build_query_conversation_summarizer(
        provider=provider,
        model=model,
    )
    query_evaluator_max_attempts = _int_env("QUERY_EVALUATOR_MAX_ATTEMPTS", 2)
    return AnswerQueryUseCase(
        wiki_repository=wiki_repository or PostgresWikiRepository(),
        markdown_reader=MinioWikiMarkdownReader(),
        event_publisher=event_publisher or NoOpQueryEventPublisher(),
        embedding_search=_build_embedding_search(text_search),
        text_search=text_search,
        answer_generator=answer_generator,
        query_rewriter=RuleBasedQueryRewriter(),
        query_evaluator=query_evaluator,
        web_search=web_search,
        query_answer_assembler=query_answer_assembler,
        query_evaluator_graph=LangGraphQueryEvaluatorGraph(
            query_answer_assembler=query_answer_assembler,
            query_evaluator=query_evaluator,
            web_search_available=web_search is not None,
            max_attempts=query_evaluator_max_attempts,
        ),
        min_internal_relevance_score=_float_env("QUERY_MIN_INTERNAL_RELEVANCE_SCORE", 0.5),
        query_evaluator_max_attempts=query_evaluator_max_attempts,
        max_evidence_snippets=_int_env("QUERY_EVIDENCE_LIMIT", 8),
        conversation_summarizer=conversation_summarizer,
    )


def get_query_answer_use_case(payload: QueryRequest) -> AnswerQueryUseCase:
    return build_answer_query_use_case(
        provider=payload.provider,
        model=payload.model,
        allow_web_search=payload.allow_web_search,
    )


def _build_embedding_search(text_search: Bm25Searcher):
    mode = os.environ.get("QUERY_EMBEDDING_MODE", "bge-m3").strip().lower()
    if mode in {"text-only", "bm25", "lexical"}:
        return text_search
    return _stored_embedding_search()


@lru_cache(maxsize=1)
def _stored_embedding_search() -> StoredWikiPageEmbeddingSearch:
    return StoredWikiPageEmbeddingSearch()


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
