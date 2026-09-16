from dataclasses import dataclass, replace

from app.modules.query.application.build_query_context import BuildQueryContextUseCase
from app.modules.query.application.conversation_context_resolver import (
    contextualize_question,
    evidence_question,
    update_conversation_summary,
)
from app.modules.query.application.ports import (
    AnswerGeneratorPort,
    ConversationSummarizerPort,
    EmbeddingSearchPort,
    QueryEventPublisherPort,
    QueryEvaluatorGraphPort,
    QueryEvaluatorPort,
    QueryRewritePort,
    SemanticQueryEmbeddingPort,
    TextSearchPort,
    WebSearchPort,
    WikiMarkdownReaderPort,
    WikiRepositoryPort,
)
from app.modules.query.application.query_answer_assembler import QueryAnswerAssembler
from app.modules.query.application.query_event import publish_query_event
from app.modules.query.application.query_evaluator_flow import (
    QueryEvaluatorLoop,
    apply_evidence_sufficiency_boundary,
)
from app.modules.query.application.query_graph_paths import (
    add_focus_concepts_to_related_pages,
    add_sources_connected_to_focus_concepts,
    backfill_direct_concept_paths,
    select_answer_paths,
)
from app.modules.query.application.query_page_scorer import QueryPageScorer
from app.modules.query.application.query_web_answer_builder import QueryWebAnswerBuilder
from app.modules.query.application.retrieval_summary import build_retrieval_summary
from app.modules.query.application.evidence_selector import has_selectable_evidence
from app.modules.query.application.traverse_wiki_graph import TraverseWikiGraphUseCase
from app.modules.query.domain.entities import (
    ConversationContext,
    EvidenceSnippet,
    GeneratedAnswer,
    GraphContext,
    OutputLanguage,
    QueryAnswer,
    QueryContext,
    QueryEvaluation,
    QueryRetrieval,
    QueryRewrite,
    ResponseLength,
    RetrievedPage,
    TraversalPath,
    WebSearchTelemetry,
    WikiPage,
    WikiPageLink,
)
from app.modules.query.domain.value_objects import Question


@dataclass(frozen=True)
class _ScoredWikiCandidates:
    pages_by_id: dict[str, WikiPage]
    links: list[WikiPageLink]
    source_pages: list[WikiPage]
    concept_pages: list[WikiPage]
    source_scores: dict[str, float]
    concept_scores: dict[str, float]
    direct_concept_ids: list[str]


@dataclass(frozen=True)
class _InternalRetrievalGraph:
    related_pages: list[RetrievedPage]
    graph_context: GraphContext
    traversal_paths: list[TraversalPath]
    stop_reason: str


@dataclass(frozen=True)
class _InternalQueryContext:
    query_context: QueryContext
    related_pages: list[RetrievedPage]
    graph_context: GraphContext
    traversal_paths: list[TraversalPath]
    stop_reason: str


class AnswerQueryUseCase:
    def __init__(
        self,
        wiki_repository: WikiRepositoryPort,
        embedding_search: EmbeddingSearchPort,
        text_search: TextSearchPort,
        answer_generator: AnswerGeneratorPort,
        markdown_reader: WikiMarkdownReaderPort | None = None,
        event_publisher: QueryEventPublisherPort | None = None,
        query_rewriter: QueryRewritePort | None = None,
        query_evaluator: QueryEvaluatorPort | None = None,
        web_search: WebSearchPort | None = None,
        traverse_wiki_graph: TraverseWikiGraphUseCase | None = None,
        build_query_context: BuildQueryContextUseCase | None = None,
        query_answer_assembler: QueryAnswerAssembler | None = None,
        query_page_scorer: QueryPageScorer | None = None,
        query_web_answer_builder: QueryWebAnswerBuilder | None = None,
        query_evaluator_graph: QueryEvaluatorGraphPort | None = None,
        source_candidate_limit: int = 15,
        concept_candidate_limit: int = 10,
        focus_concept_threshold: float = 0.45,
        returned_path_limit: int = 5,
        min_internal_relevance_score: float = 0.0,
        query_evaluator_max_attempts: int = 2,
        candidate_pool_multiplier: int = 4,
        graph_link_limit: int = 200,
        graph_expansion_depth: int = 3,
        max_evidence_snippets: int = 8,
        conversation_summarizer: ConversationSummarizerPort | None = None,
    ) -> None:
        self._wiki_repository = wiki_repository
        self._embedding_search = embedding_search
        self._text_search = text_search
        self._answer_generator = answer_generator
        self._markdown_reader = markdown_reader
        self._event_publisher = event_publisher
        self._query_rewriter = query_rewriter
        self._query_evaluator = query_evaluator
        self._web_search = web_search
        self._traverse_wiki_graph = traverse_wiki_graph or TraverseWikiGraphUseCase()
        self._build_query_context = build_query_context or BuildQueryContextUseCase(
            embedding_search=embedding_search,
            text_search=text_search,
            max_evidence_snippets=max_evidence_snippets,
        )
        self._query_answer_assembler = query_answer_assembler or QueryAnswerAssembler(answer_generator)
        self._query_page_scorer = query_page_scorer or QueryPageScorer(
            embedding_search=embedding_search,
            text_search=text_search,
            source_candidate_limit=source_candidate_limit,
            concept_candidate_limit=concept_candidate_limit,
            focus_concept_threshold=focus_concept_threshold,
            score_source_structures=not isinstance(
                embedding_search,
                SemanticQueryEmbeddingPort,
            ),
        )
        self._query_web_answer_builder = query_web_answer_builder
        if self._query_web_answer_builder is None and web_search is not None:
            self._query_web_answer_builder = QueryWebAnswerBuilder(
                web_search=web_search,
                build_query_context=self._build_query_context,
                query_answer_assembler=self._query_answer_assembler,
                embedding_unit_loader=self._load_embedding_units_for_related_pages,
            )
        self._source_candidate_limit = source_candidate_limit
        self._concept_candidate_limit = concept_candidate_limit
        self._focus_concept_threshold = focus_concept_threshold
        self._returned_path_limit = returned_path_limit
        self._min_internal_relevance_score = min_internal_relevance_score
        self._query_evaluator_max_attempts = max(1, query_evaluator_max_attempts)
        self._candidate_pool_multiplier = max(1, candidate_pool_multiplier)
        self._graph_link_limit = max(1, graph_link_limit)
        self._graph_expansion_depth = max(1, graph_expansion_depth)
        self._conversation_summarizer = conversation_summarizer
        self._query_evaluator_graph = query_evaluator_graph or QueryEvaluatorLoop(
            query_answer_assembler=self._query_answer_assembler,
            query_evaluator=query_evaluator,
            web_search_available=web_search is not None,
            max_attempts=self._query_evaluator_max_attempts,
        )

    def execute(
        self,
        question: str,
        *,
        workspace_id: str,
        user_id: str | None = None,
        event_publisher: QueryEventPublisherPort | None = None,
        conversation_context: ConversationContext | None = None,
        output_language: OutputLanguage | None = None,
        response_length: ResponseLength | None = None,
        allow_web_search: bool | None = None,
    ) -> QueryAnswer:
        updated_summary = update_conversation_summary(
            conversation_context,
            self._conversation_summarizer,
        )
        result = self._execute(
            question,
            workspace_id=workspace_id,
            user_id=user_id,
            event_publisher=event_publisher,
            conversation_context=conversation_context,
            output_language=output_language,
            response_length=response_length,
            allow_web_search=allow_web_search,
        )
        return replace(result, updated_conversation_summary=updated_summary)

    def retrieve_evidence(
        self,
        question: str,
        *,
        workspace_id: str,
        user_id: str | None = None,
    ) -> QueryRetrieval:
        normalized_question = Question(question).normalized
        query_rewrite = self._rewrite_query(normalized_question)
        candidates = self._score_wiki_candidates(
            workspace_id,
            query_rewrite,
            None,
        )
        context = self._build_internal_query_context(
            original_question=normalized_question,
            contextual_question=normalized_question,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_context=None,
            output_language=None,
            response_length=None,
            allow_web_search=False,
            candidates=candidates,
            event_publisher=None,
        )
        return QueryRetrieval(
            evidence_snippets=context.query_context.evidence_snippets,
            retrieval_summary=build_retrieval_summary(
                related_pages=context.related_pages,
                source_candidate_count=min(
                    len(candidates.source_pages),
                    self._source_candidate_limit,
                ),
                concept_candidate_count=min(
                    len(candidates.concept_pages),
                    self._concept_candidate_limit,
                ),
                stop_reason=context.stop_reason,
            ),
        )

    def _execute(
        self,
        question: str,
        *,
        workspace_id: str,
        user_id: str | None = None,
        event_publisher: QueryEventPublisherPort | None = None,
        conversation_context: ConversationContext | None = None,
        output_language: OutputLanguage | None = None,
        response_length: ResponseLength | None = None,
        allow_web_search: bool | None = None,
    ) -> QueryAnswer:
        event_publisher = event_publisher or self._event_publisher
        query = Question(question)
        web_search_telemetry = WebSearchTelemetry()
        self._publish(event_publisher, "query_started", "질의 처리를 시작했습니다.", {"question": query.normalized})
        contextual_question = contextualize_question(query.normalized, conversation_context)
        if contextual_question != query.normalized:
            self._publish(
                event_publisher,
                "query_contextualized",
                "대화 요약과 참조 메모리로 검색용 질문을 보강했습니다.",
                {"contextual_question": contextual_question},
            )
        query_rewrite = self._rewrite_query(contextual_question)
        if query_rewrite.retrieval_query != query.normalized:
            self._publish(
                event_publisher,
                "query_rewritten",
                "검색용 질의를 정제했습니다.",
                {"retrieval_query": query_rewrite.retrieval_query, "keywords": query_rewrite.keywords},
            )
        candidates = self._score_wiki_candidates(
            workspace_id,
            query_rewrite,
            event_publisher,
        )

        web_search_allowed = allow_web_search is not False
        low_internal_relevance = self._should_use_web_fallback(
            candidates.source_scores,
            candidates.concept_scores,
        )
        if (
            web_search_allowed
            and self._query_evaluator is None
            and low_internal_relevance
        ):
            self._mark_web_search_requested(web_search_telemetry)
            fallback_answer = self._answer_from_web_search(
                query.normalized,
                query_rewrite,
                event_publisher,
                output_language,
                response_length,
                web_search_telemetry,
            )
            if fallback_answer is not None:
                return fallback_answer

        internal_context = self._build_internal_query_context(
            original_question=query.normalized,
            contextual_question=contextual_question,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_context=conversation_context,
            candidates=candidates,
            event_publisher=event_publisher,
            output_language=output_language,
            response_length=response_length,
            allow_web_search=allow_web_search,
        )
        answer, evidence_snippets, evaluated_context, query_evaluation = self._query_evaluator_graph.run(
            question=query.normalized,
            query_context=internal_context.query_context,
            stop_reason=internal_context.stop_reason,
            event_publisher=event_publisher,
        )
        if query_evaluation is None and low_internal_relevance:
            query_evaluation = QueryEvaluation(
                route="unsupported",
                reason="내부 근거의 relevance가 정책 기준보다 낮고 웹 검색이 비활성화되었습니다.",
            )
        if query_evaluation is not None:
            query_evaluation = apply_evidence_sufficiency_boundary(
                query_evaluation,
                has_internal_evidence=bool(evidence_snippets),
                web_search_available=web_search_allowed,
            )
            if web_search_allowed and query_evaluation.route == "web_fallback":
                self._mark_web_search_requested(web_search_telemetry)
                fallback_answer = self._answer_from_web_search(
                    query.normalized,
                    self._query_rewrite_for_web(query_rewrite, query_evaluation),
                    event_publisher,
                    output_language,
                    response_length,
                    web_search_telemetry,
                )
                if fallback_answer is not None:
                    return fallback_answer
                query_evaluation = replace(query_evaluation, route="unsupported")
            if web_search_allowed and query_evaluation.route == "internal_web_augmented":
                self._mark_web_search_requested(web_search_telemetry)
                augmented_answer = self._answer_from_internal_web_augmented(
                    question=query.normalized,
                    query_rewrite=self._query_rewrite_for_web(query_rewrite, query_evaluation),
                    query_context=internal_context.query_context,
                    graph_context=internal_context.graph_context,
                    traversal_paths=internal_context.traversal_paths,
                    stop_reason="internal_web_augmented",
                    event_publisher=event_publisher,
                    web_search_telemetry=web_search_telemetry,
                )
                if augmented_answer is not None:
                    return augmented_answer
                query_evaluation = replace(query_evaluation, route="unsupported")
        answer, evidence_snippets, stop_reason = self._apply_evaluation_route(
            query_evaluation,
            answer,
            evidence_snippets,
            internal_context.stop_reason,
            output_language,
            query.normalized,
        )

        summary = build_retrieval_summary(
            related_pages=internal_context.related_pages,
            source_candidate_count=min(
                len(candidates.source_pages),
                self._source_candidate_limit,
            ),
            concept_candidate_count=min(
                len(candidates.concept_pages),
                self._concept_candidate_limit,
            ),
            stop_reason=stop_reason,
        )
        return QueryAnswer(
            answer=answer,
            related_pages=internal_context.related_pages,
            evidence_snippets=evidence_snippets,
            graph_context=internal_context.graph_context,
            traversal_paths=internal_context.traversal_paths,
            retrieval_summary=summary,
            web_search=web_search_telemetry,
            evaluation=query_evaluation,
        )

    def _build_internal_query_context(
        self,
        *,
        original_question: str,
        contextual_question: str,
        workspace_id: str,
        user_id: str | None,
        conversation_context: ConversationContext | None,
        output_language: OutputLanguage | None,
        response_length: ResponseLength | None,
        allow_web_search: bool | None,
        candidates: _ScoredWikiCandidates,
        event_publisher: QueryEventPublisherPort | None,
    ) -> _InternalQueryContext:
        retrieval_graph = self._build_internal_retrieval_graph(
            candidates,
            event_publisher,
        )
        related_pages = self._load_markdown_for_related_pages(
            retrieval_graph.related_pages
        )
        graph_context = GraphContext(
            nodes=related_pages,
            edges=retrieval_graph.graph_context.edges,
        )
        self._publish(
            event_publisher,
            "markdown_loaded",
            "선택된 Wiki page의 Markdown 본문을 로드했습니다.",
            {
                "loaded_markdown_count": len(
                    [item for item in related_pages if item.page.markdown]
                )
            },
        )
        query_context = self._build_query_context.execute(
            question=contextual_question,
            related_pages=related_pages,
            graph_context=graph_context,
            traversal_paths=retrieval_graph.traversal_paths,
            original_question=original_question,
            evidence_question=evidence_question(
                original_question,
                conversation_context,
                contextual_question,
            ),
            embedding_units_by_page_id=self._load_embedding_units_for_related_pages(
                related_pages
            ),
            workspace_id=workspace_id,
            user_id=user_id,
            output_language=output_language,
            response_length=response_length,
            allow_web_search=allow_web_search,
        )
        related_page_ids = {item.page.id for item in related_pages}
        top_concept_score = max(candidates.concept_scores.values(), default=0.0)
        fallback_score_floor = (
            0.0
            if not query_context.evidence_snippets
            else max(0.0, top_concept_score - 0.15)
        )
        if not query_context.evidence_snippets or any(
            page.id not in related_page_ids
            and candidates.concept_scores.get(page.id, 0.0) >= fallback_score_floor
            for page in candidates.concept_pages
        ):
            fallback_concepts = [
                RetrievedPage(
                    page=page,
                    score=candidates.concept_scores.get(page.id, 0.0),
                    role="focus_concept",
                )
                for page in sorted(
                    candidates.concept_pages,
                    key=lambda item: candidates.concept_scores.get(item.id, 0.0),
                    reverse=True,
                )
                if page.id not in related_page_ids
                and candidates.concept_scores.get(page.id, 0.0) > 0
                and candidates.concept_scores.get(page.id, 0.0) >= fallback_score_floor
            ][: self._concept_candidate_limit]
            if fallback_concepts:
                fallback_concepts = self._load_markdown_for_related_pages(
                    fallback_concepts
                )
                fallback_units = self._load_embedding_units_for_related_pages(
                    fallback_concepts
                ) or {}
                fallback_concepts = [
                    item
                    for item in fallback_concepts
                    if has_selectable_evidence(
                        item.page,
                        fallback_units.get(item.page.id, []),
                    )
                ]
                if fallback_concepts:
                    related_pages = [*related_pages, *fallback_concepts]
                    graph_context = replace(graph_context, nodes=related_pages)
                    query_context = self._build_query_context.execute(
                        question=contextual_question,
                        related_pages=related_pages,
                        graph_context=graph_context,
                        traversal_paths=retrieval_graph.traversal_paths,
                        original_question=original_question,
                        evidence_question=evidence_question(
                            original_question,
                            conversation_context,
                            contextual_question,
                        ),
                        embedding_units_by_page_id={
                            **self._load_embedding_units_for_related_pages(related_pages),
                            **fallback_units,
                        },
                        workspace_id=workspace_id,
                        user_id=user_id,
                        output_language=output_language,
                        response_length=response_length,
                        allow_web_search=allow_web_search,
                    )
        self._publish(
            event_publisher,
            "context_built",
            "LLM 답변 입력 context를 구성했습니다.",
            {
                "context_chars": len(query_context.answer_context),
                "related_page_count": len(related_pages),
            },
        )
        return _InternalQueryContext(
            query_context=query_context,
            related_pages=related_pages,
            graph_context=graph_context,
            traversal_paths=retrieval_graph.traversal_paths,
            stop_reason=retrieval_graph.stop_reason,
        )

    def _build_internal_retrieval_graph(
        self,
        candidates: _ScoredWikiCandidates,
        event_publisher: QueryEventPublisherPort | None,
    ) -> _InternalRetrievalGraph:
        seed_source_ids = self._query_page_scorer.select_seed_sources(
            candidates.source_pages,
            candidates.source_scores,
        )
        focus_concept_ids = self._query_page_scorer.select_focus_concepts(
            candidates.concept_pages,
            candidates.concept_scores,
        )
        if (
            candidates.direct_concept_ids
            and max(candidates.source_scores.values(), default=0.0)
            < self._focus_concept_threshold
        ):
            seed_source_ids = add_sources_connected_to_focus_concepts(
                seed_source_ids,
                candidates.direct_concept_ids,
                candidates.links,
            )
        seed_page_ids = list(
            dict.fromkeys(
                [
                    *seed_source_ids,
                    *focus_concept_ids,
                    *candidates.direct_concept_ids,
                ]
            )
        )
        self._publish(
            event_publisher,
            "seeds_selected",
            "탐색 시작 source와 focus concept hint를 선택했습니다.",
            {
                "seed_page_ids": seed_page_ids,
                "seed_source_ids": seed_source_ids,
                "focus_concept_ids": focus_concept_ids,
                "direct_concept_ids": candidates.direct_concept_ids,
            },
        )
        graph_context, traversal_paths, stop_reason = self._traverse_wiki_graph.execute(
            pages_by_id=candidates.pages_by_id,
            links=candidates.links,
            seed_page_ids=seed_page_ids,
            node_scores={**candidates.source_scores, **candidates.concept_scores},
        )
        self._publish(
            event_publisher,
            "graph_traversed",
            "Wiki graph traversal을 완료했습니다.",
            {
                "visited_node_count": len(graph_context.nodes),
                "path_count": len(traversal_paths),
                "stop_reason": stop_reason,
            },
        )
        traversal_paths = select_answer_paths(
            traversal_paths,
            self._returned_path_limit,
        )
        related_pages = add_focus_concepts_to_related_pages(
            graph_context.nodes,
            candidates.direct_concept_ids,
            candidates.pages_by_id,
            candidates.concept_scores,
        )
        if candidates.direct_concept_ids and {
            item.page.id for item in related_pages
        } & set(candidates.direct_concept_ids):
            stop_reason = "concept_direct_match"
        graph_context, traversal_paths = backfill_direct_concept_paths(
            graph_context=GraphContext(nodes=related_pages, edges=graph_context.edges),
            traversal_paths=traversal_paths,
            links=candidates.links,
            direct_concept_ids=candidates.direct_concept_ids,
            source_scores=candidates.source_scores,
            concept_scores=candidates.concept_scores,
        )
        if stop_reason == "concept_direct_match":
            traversal_paths = [
                replace(path, stop_reason="concept_direct_match")
                if set(path.nodes) & set(candidates.direct_concept_ids)
                else path
                for path in traversal_paths
            ]
        return _InternalRetrievalGraph(
            related_pages=related_pages,
            graph_context=GraphContext(
                nodes=related_pages,
                edges=graph_context.edges,
            ),
            traversal_paths=traversal_paths,
            stop_reason=stop_reason,
        )

    def _score_wiki_candidates(
        self,
        workspace_id: str,
        query_rewrite: QueryRewrite,
        event_publisher: QueryEventPublisherPort | None,
    ) -> _ScoredWikiCandidates:
        candidate_pages = self._wiki_repository.list_candidate_pages(
            workspace_id,
            query_rewrite.retrieval_query,
            self._source_candidate_limit * self._candidate_pool_multiplier,
            self._concept_candidate_limit * self._candidate_pool_multiplier,
            semantic_query=(
                self._embedding_search.embed_query(query_rewrite.original_question)
                if isinstance(self._embedding_search, SemanticQueryEmbeddingPort)
                else None
            ),
        )
        candidate_page_ids = [page.id for page in candidate_pages]
        seen_page_ids = set(candidate_page_ids)
        frontier_page_ids = set(candidate_page_ids)
        links_by_key: dict[tuple[str, str, str], WikiPageLink] = {}
        for _depth in range(self._graph_expansion_depth):
            remaining_link_limit = self._graph_link_limit - len(links_by_key)
            if not frontier_page_ids or remaining_link_limit <= 0:
                break
            links = self._wiki_repository.list_links_for_page_ids(
                workspace_id,
                sorted(frontier_page_ids),
                remaining_link_limit,
                excluded_page_ids=sorted(seen_page_ids - frontier_page_ids),
            )
            next_frontier: set[str] = set()
            for link in links:
                links_by_key[
                    (link.from_page_id, link.to_page_id, link.link_type)
                ] = link
                next_frontier.update(
                    {link.from_page_id, link.to_page_id} - seen_page_ids
                )
            seen_page_ids.update(next_frontier)
            frontier_page_ids = next_frontier
        links = list(links_by_key.values())
        neighbor_pages = self._wiki_repository.list_pages_by_ids(
            workspace_id,
            sorted(seen_page_ids - set(candidate_page_ids)),
        )
        pages = list(
            {
                page.id: page
                for page in [*candidate_pages, *neighbor_pages]
            }.values()
        )
        self._publish(
            event_publisher,
            "wiki_loaded",
            "활성 Wiki page/link를 로드했습니다.",
            {"page_count": len(pages), "link_count": len(links)},
        )
        pages = self._load_markdown_for_scoring(pages)
        self._publish(
            event_publisher,
            "retrieval_markdown_loaded",
            "검색용 Wiki Markdown 본문을 로드했습니다.",
            {"loaded_markdown_count": len([page for page in pages if page.markdown])},
        )
        candidate_page_id_set = set(candidate_page_ids)
        source_pages = [
            page
            for page in pages
            if page.is_source and page.id in candidate_page_id_set
        ]
        concept_pages = [
            page
            for page in pages
            if page.is_concept and page.id in candidate_page_id_set
        ]
        source_scores = self._query_page_scorer.score_pages(
            query_rewrite,
            [page for page in pages if page.is_source],
            embedding_weight=0.8,
        )
        concept_scores = self._query_page_scorer.score_pages(
            query_rewrite,
            [page for page in pages if page.is_concept],
            embedding_weight=0.8,
        )
        self._publish(
            event_publisher,
            "retrieval_scored",
            "source/concept 후보 점수를 계산했습니다.",
            {
                "source_count": len(source_scores),
                "concept_count": len(concept_scores),
                "top_source_id": self._top_id(source_scores),
                "top_concept_id": self._top_id(concept_scores),
                "top_sources": self._top_scores(source_scores),
                "top_concepts": self._top_scores(concept_scores),
            },
        )
        return _ScoredWikiCandidates(
            pages_by_id={page.id: page for page in pages},
            links=links,
            source_pages=source_pages,
            concept_pages=concept_pages,
            source_scores=source_scores,
            concept_scores=concept_scores,
            direct_concept_ids=self._query_page_scorer.select_direct_match_concepts(
                query_rewrite,
                concept_pages,
            ),
        )

    def _rewrite_query(self, question: str) -> QueryRewrite:
        if self._query_rewriter is None:
            return QueryRewrite(original_question=question, retrieval_query=question)
        try:
            rewritten = self._query_rewriter.rewrite(question)
        except Exception:
            return QueryRewrite(original_question=question, retrieval_query=question)
        if not rewritten.retrieval_query.strip():
            return QueryRewrite(original_question=question, retrieval_query=question)
        return rewritten

    def _apply_evaluation_route(
        self,
        evaluation: QueryEvaluation | None,
        answer: GeneratedAnswer,
        evidence_snippets: list[EvidenceSnippet],
        stop_reason: str,
        output_language: OutputLanguage | None,
        question: str,
    ) -> tuple[GeneratedAnswer, list[EvidenceSnippet], str]:
        if evaluation is None:
            if stop_reason != "no_relevant_seed":
                return answer, evidence_snippets, stop_reason
            unsupported = self._unsupported_answer(evidence_snippets, output_language, question)
            unsupported, evidence_snippets = self._query_answer_assembler.renumber_used_evidence(
                unsupported,
                evidence_snippets,
            )
            return unsupported, evidence_snippets, stop_reason

        if evaluation.route == "internal_supported" and stop_reason == "no_relevant_seed":
            return answer, evidence_snippets, "query_evaluator_internal_supported"
        if evaluation.route not in {"unsupported", "revise_answer"}:
            return answer, evidence_snippets, stop_reason

        unsupported = self._unsupported_answer(evidence_snippets, output_language, question)
        unsupported, evidence_snippets = self._query_answer_assembler.renumber_used_evidence(
            unsupported,
            evidence_snippets,
        )
        routed_stop_reason = (
            "query_evaluator_unsupported"
            if evaluation.route == "unsupported"
            else "query_evaluator_unresolved"
        )
        return unsupported, evidence_snippets, routed_stop_reason

    def _query_rewrite_for_web(self, query_rewrite: QueryRewrite, evaluation: QueryEvaluation) -> QueryRewrite:
        if not evaluation.web_query:
            return query_rewrite
        return QueryRewrite(
            original_question=query_rewrite.original_question,
            retrieval_query=evaluation.web_query,
            keywords=evaluation.web_query.split(),
        )

    def _should_use_web_fallback(self, source_scores: dict[str, float], concept_scores: dict[str, float]) -> bool:
        if self._min_internal_relevance_score <= 0:
            return False
        best_score = max([*source_scores.values(), *concept_scores.values(), 0.0])
        return best_score < self._min_internal_relevance_score

    def _mark_web_search_requested(self, telemetry: WebSearchTelemetry) -> None:
        telemetry.requested = True
        if self._web_search is None:
            telemetry.error_code = "web_search_unavailable"

    def _answer_from_web_search(
        self,
        question: str,
        query_rewrite: QueryRewrite,
        event_publisher: QueryEventPublisherPort | None,
        output_language: OutputLanguage | None,
        response_length: ResponseLength | None,
        web_search_telemetry: WebSearchTelemetry,
    ) -> QueryAnswer | None:
        if self._query_web_answer_builder is None:
            return None
        return self._query_web_answer_builder.answer_from_web_search(
            question=question,
            query_rewrite=query_rewrite,
            event_publisher=event_publisher,
            output_language=output_language,
            response_length=response_length,
            web_search_telemetry=web_search_telemetry,
        )

    def _answer_from_internal_web_augmented(
        self,
        question: str,
        query_rewrite: QueryRewrite,
        query_context: QueryContext,
        graph_context: GraphContext,
        traversal_paths: list[TraversalPath],
        stop_reason: str,
        event_publisher: QueryEventPublisherPort | None,
        web_search_telemetry: WebSearchTelemetry,
    ) -> QueryAnswer | None:
        if self._query_web_answer_builder is None:
            return None
        return self._query_web_answer_builder.answer_from_internal_web_augmented(
            question=question,
            query_rewrite=query_rewrite,
            query_context=query_context,
            graph_context=graph_context,
            traversal_paths=traversal_paths,
            stop_reason=stop_reason,
            event_publisher=event_publisher,
            web_search_telemetry=web_search_telemetry,
        )

    def _top_id(self, scores: dict[str, float]) -> str | None:
        if not scores:
            return None
        return max(scores, key=scores.get)

    def _top_scores(self, scores: dict[str, float], limit: int = 5) -> list[dict[str, object]]:
        return [
            {"id": page_id, "score": round(score, 4)}
            for page_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
        ]

    def _unsupported_answer(
        self,
        evidence_snippets: list[EvidenceSnippet],
        output_language: OutputLanguage | None,
        question: str,
    ) -> GeneratedAnswer:
        reference_text = evidence_snippets[0].text if evidence_snippets else question
        language = _fallback_language(output_language, reference_text, question)
        no_evidence, with_evidence = {
            "en": (
                "The provided evidence does not directly answer the question.",
                "The provided evidence does not directly answer the question. "
                "The closest evidence also does not directly explain the topic.",
            ),
            "ja": (
                "提供された根拠には、質問に直接答える内容がありません。",
                "提供された根拠には、質問に直接答える内容がありません。"
                "最も近い根拠も質問の主題を直接説明していません。",
            ),
            "zh": (
                "提供的证据中没有能够直接回答问题的内容。",
                "提供的证据中没有能够直接回答问题的内容。"
                "最接近的证据也没有直接解释该主题。",
            ),
            "ko": (
                "제공된 근거에서 질문에 직접 답할 내용을 찾지 못했습니다.",
                "제공된 근거에서 질문에 직접 답할 내용을 찾지 못했습니다. "
                "가장 가까운 근거도 질문 주제를 직접 설명하지 않습니다.",
            ),
        }[language]
        if not evidence_snippets:
            return GeneratedAnswer(content=no_evidence)
        nearest = evidence_snippets[0]
        return GeneratedAnswer(content=f"{with_evidence} [{nearest.rank}]")

    def _load_markdown_for_related_pages(self, related_pages: list[RetrievedPage]) -> list[RetrievedPage]:
        if self._markdown_reader is None:
            return related_pages

        loaded: list[RetrievedPage] = []
        for item in related_pages:
            page = item.page
            if page.markdown or not page.markdown_uri:
                loaded.append(item)
                continue
            try:
                markdown = self._markdown_reader.read_markdown(page.markdown_uri)
            except Exception:
                loaded.append(item)
                continue
            loaded.append(replace(item, page=replace(page, markdown=markdown)))
        return loaded

    def _load_embedding_units_for_related_pages(self, related_pages: list[RetrievedPage]) -> dict[str, list]:
        loader = getattr(self._wiki_repository, "list_embedding_units_by_page_ids", None)
        if loader is None:
            return {}
        page_ids = [item.page.id for item in related_pages if item.page.page_type != "web"]
        if not page_ids:
            return {}
        try:
            return loader(page_ids) or {}
        except Exception:
            return {}

    def _load_markdown_for_scoring(self, pages: list[WikiPage]) -> list[WikiPage]:
        if self._markdown_reader is None:
            return pages

        loaded: list[WikiPage] = []
        for page in pages:
            if page.markdown or not page.markdown_uri:
                loaded.append(page)
                continue
            try:
                loaded.append(replace(page, markdown=self._markdown_reader.read_markdown(page.markdown_uri)))
            except Exception:
                loaded.append(page)
        return loaded

    def _publish(
        self,
        event_publisher: QueryEventPublisherPort | None,
        stage: str,
        message: str,
        data: dict[str, object] | None = None,
    ) -> None:
        publish_query_event(event_publisher, stage, message, data)


def _fallback_language(output_language: OutputLanguage | None, reference_text: str, question: str) -> str:
    if output_language == "en":
        return "en"
    if output_language != "document":
        return "ko"
    if any("가" <= char <= "힣" for char in reference_text):
        return "ko"
    if any("ぁ" <= char <= "ヿ" for char in reference_text):
        return "ja"
    # `场`은 일본어 상용 한자에 없는 간체자이므로 중국어 문서를 확정한다.
    if "场" in reference_text:
        return "zh"
    if any("가" <= char <= "힣" for char in question):
        return "ko"
    if any("ぁ" <= char <= "ヿ" for char in question):
        return "ja"
    if any("一" <= char <= "鿿" for char in question):
        return "zh"
    return "en"
