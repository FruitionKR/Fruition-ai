import hashlib
import re
from collections.abc import Callable

from app.modules.query.application.build_query_context import BuildQueryContextUseCase
from app.modules.query.application.ports import QueryEventPublisherPort, WebSearchPort
from app.modules.query.application.query_answer_assembler import QueryAnswerAssembler
from app.modules.query.application.query_event import publish_query_event
from app.modules.query.application.retrieval_summary import build_retrieval_summary
from app.modules.query.domain.entities import (
    GraphContext,
    OutputLanguage,
    QueryAnswer,
    QueryContext,
    QueryRewrite,
    ResponseLength,
    RetrievedPage,
    TraversalPath,
    WebSearchResult,
    WebSearchTelemetry,
    WikiEmbeddingUnit,
    WikiPage,
)


class QueryWebAnswerBuilder:
    def __init__(
        self,
        web_search: WebSearchPort,
        build_query_context: BuildQueryContextUseCase,
        query_answer_assembler: QueryAnswerAssembler,
        embedding_unit_loader: Callable[[list[RetrievedPage]], dict[str, list[WikiEmbeddingUnit]]] | None = None,
    ) -> None:
        self._web_search = web_search
        self._build_query_context = build_query_context
        self._query_answer_assembler = query_answer_assembler
        self._embedding_unit_loader = embedding_unit_loader or (lambda related_pages: {})

    def answer_from_web_search(
        self,
        question: str,
        query_rewrite: QueryRewrite,
        event_publisher: QueryEventPublisherPort | None,
        output_language: OutputLanguage | None = None,
        response_length: ResponseLength | None = None,
        web_search_telemetry: WebSearchTelemetry | None = None,
    ) -> QueryAnswer | None:
        web_search_telemetry = web_search_telemetry or WebSearchTelemetry(requested=True)
        web_results = self._search_web(
            query_rewrite.retrieval_query,
            event_publisher,
            web_search_telemetry,
            started_message="내부 Wiki 근거가 부족해 웹 검색 fallback을 시작했습니다.",
            failed_message="웹 검색 fallback이 실패했습니다.",
            empty_message="웹 검색 fallback 결과가 없습니다.",
        )
        if web_results is None:
            return None

        related_pages = self.web_results_to_related_pages(web_results)
        graph_context = GraphContext(nodes=related_pages, edges=[])
        query_context = self._build_query_context.execute(
            question=question,
            related_pages=related_pages,
            graph_context=graph_context,
            traversal_paths=[],
            answer_mode="web_fallback",
            output_language=output_language,
            response_length=response_length,
            allow_web_search=True,
        )
        answer, evidence_snippets = self._query_answer_assembler.generate_supported_answer(query_context)
        self._publish(
            event_publisher,
            "web_search_answer_generated",
            "웹 검색 근거로 답변 생성을 완료했습니다.",
            {
                **self._telemetry_data(web_search_telemetry),
                "answer_chars": len(answer.content),
            },
        )
        return QueryAnswer(
            answer=answer,
            related_pages=related_pages,
            evidence_snippets=evidence_snippets,
            graph_context=graph_context,
            traversal_paths=[],
            retrieval_summary=build_retrieval_summary(
                related_pages=related_pages,
                source_candidate_count=0,
                concept_candidate_count=0,
                stop_reason="web_search_fallback",
            ),
            web_search=web_search_telemetry,
        )

    def answer_from_internal_web_augmented(
        self,
        question: str,
        query_rewrite: QueryRewrite,
        query_context: QueryContext,
        graph_context: GraphContext,
        traversal_paths: list[TraversalPath],
        stop_reason: str,
        event_publisher: QueryEventPublisherPort | None,
        web_search_telemetry: WebSearchTelemetry | None = None,
    ) -> QueryAnswer | None:
        web_search_telemetry = web_search_telemetry or WebSearchTelemetry(requested=True)
        web_results = self._search_web(
            query_rewrite.retrieval_query,
            event_publisher,
            web_search_telemetry,
            started_message="내부 Wiki 근거에 외부 검색 근거를 보강합니다.",
            failed_message="웹 검색 보강이 실패했습니다.",
            empty_message="웹 검색 보강 결과가 없습니다.",
        )
        if web_results is None:
            return None

        web_related_pages = self.web_results_to_related_pages(web_results)
        related_pages = self._merge_related_pages(query_context.related_pages, web_related_pages)
        augmented_graph_context = GraphContext(nodes=related_pages, edges=graph_context.edges)
        augmented_context = self._build_query_context.execute(
            question=query_context.question,
            related_pages=related_pages,
            graph_context=augmented_graph_context,
            traversal_paths=traversal_paths,
            original_question=question,
            answer_mode="internal_web_augmented",
            embedding_units_by_page_id=self._embedding_unit_loader(related_pages),
            workspace_id=query_context.workspace_id,
            user_id=query_context.user_id,
            output_language=query_context.output_language,
            response_length=query_context.response_length,
            allow_web_search=query_context.allow_web_search,
        )
        answer, evidence_snippets = self._query_answer_assembler.generate_supported_answer(augmented_context)
        self._publish(
            event_publisher,
            "web_search_answer_generated",
            "내부 Wiki와 웹 검색 근거를 함께 사용해 답변 생성을 완료했습니다.",
            {
                **self._telemetry_data(web_search_telemetry),
                "answer_chars": len(answer.content),
            },
        )
        return QueryAnswer(
            answer=answer,
            related_pages=related_pages,
            evidence_snippets=evidence_snippets,
            graph_context=augmented_graph_context,
            traversal_paths=traversal_paths,
            retrieval_summary=build_retrieval_summary(
                related_pages=related_pages,
                source_candidate_count=0,
                concept_candidate_count=0,
                stop_reason=stop_reason,
            ),
            web_search=web_search_telemetry,
        )

    def web_results_to_related_pages(self, web_results: list[WebSearchResult]) -> list[RetrievedPage]:
        related_pages = []
        for index, result in enumerate(web_results, start=1):
            page = WikiPage(
                id=f"web:{self._hash(result.url)}",
                page_type="web",
                title=result.title,
                slug=self._slug(result.title) or f"web-result-{index}",
                summary=result.snippet,
                markdown_uri=result.url,
                markdown=result.content or result.snippet,
            )
            related_pages.append(
                RetrievedPage(
                    page=page,
                    score=max(0.0, min(1.0, result.score)),
                    role="web_search_result",
                    depth=0,
                )
            )
        return related_pages

    def _search_web(
        self,
        retrieval_query: str,
        event_publisher: QueryEventPublisherPort | None,
        web_search_telemetry: WebSearchTelemetry,
        started_message: str,
        failed_message: str,
        empty_message: str,
    ) -> list[WebSearchResult] | None:
        web_search_telemetry.requested = True
        self._publish(
            event_publisher,
            "web_search_started",
            started_message,
            {
                "retrieval_query": retrieval_query,
                **self._telemetry_data(web_search_telemetry),
            },
        )
        try:
            web_results = self._web_search.search(retrieval_query)
        except Exception:
            web_search_telemetry.executed = True
            web_search_telemetry.error_code = "web_search_failed"
            self._publish(
                event_publisher,
                "web_search_failed",
                failed_message,
                self._telemetry_data(web_search_telemetry),
            )
            return None
        web_search_telemetry.executed = True
        web_search_telemetry.result_count = len(web_results)
        if not web_results:
            self._publish(
                event_publisher,
                "web_search_empty",
                empty_message,
                self._telemetry_data(web_search_telemetry),
            )
            return None
        self._publish(
            event_publisher,
            "web_search_executed",
            "웹 검색 실행을 완료했습니다.",
            self._telemetry_data(web_search_telemetry),
        )
        return web_results

    def _merge_related_pages(self, internal_pages: list[RetrievedPage], web_pages: list[RetrievedPage]) -> list[RetrievedPage]:
        merged = list(internal_pages)
        seen = {item.page.id for item in merged}
        for item in web_pages:
            if item.page.id in seen:
                continue
            merged.append(item)
            seen.add(item.page.id)
        return merged

    def _hash(self, text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    def _slug(self, text: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9가-힣_.-]+", "-", text.lower()).strip("-._")
        return slug[:80]

    def _publish(
        self,
        event_publisher: QueryEventPublisherPort | None,
        stage: str,
        message: str,
        data: dict[str, object] | None = None,
    ) -> None:
        publish_query_event(event_publisher, stage, message, data)

    def _telemetry_data(self, telemetry: WebSearchTelemetry) -> dict[str, object]:
        return {
            "web_search_requested": telemetry.requested,
            "web_search_executed": telemetry.executed,
            "result_count": telemetry.result_count,
            "error_code": telemetry.error_code,
        }
