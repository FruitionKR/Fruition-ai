import unittest

from app.modules.query.application.build_query_context import BuildQueryContextUseCase
from app.modules.query.application.query_answer_assembler import QueryAnswerAssembler
from app.modules.query.application.query_web_answer_builder import QueryWebAnswerBuilder
from app.modules.query.domain.entities import (
    GeneratedAnswer,
    QueryContext,
    QueryRewrite,
    WebSearchResult,
    WebSearchTelemetry,
)


class EmptySearch:
    def score(self, query: str, documents: list[str]) -> list[float]:
        return [0.0 for _ in documents]


class FakeWebSearch:
    def __init__(self, results: list[WebSearchResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, query: str) -> list[WebSearchResult]:
        self.queries.append(query)
        return self.results


class FailingWebSearch:
    def search(self, query: str) -> list[WebSearchResult]:
        raise RuntimeError("provider detail must not be published")


class RecordingAnswerGenerator:
    def __init__(self) -> None:
        self.last_context: QueryContext | None = None

    def generate_answer(self, context: QueryContext) -> GeneratedAnswer:
        self.last_context = context
        return GeneratedAnswer(content="웹 근거로 답변합니다. [1]")


class RecordingEventPublisher:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.payloads: list[dict[str, object] | None] = []

    def publish(self, stage: str, message: str, data: dict[str, object] | None = None) -> None:
        self.events.append(stage)
        self.payloads.append(data)


class QueryWebAnswerBuilderTest(unittest.TestCase):
    def test_builds_web_fallback_answer_from_web_results(self) -> None:
        web_search = FakeWebSearch(
            [
                WebSearchResult(
                    title="Kubernetes Operator",
                    url="https://example.com/operator",
                    snippet="An operator automates Kubernetes application operations.",
                    score=0.9,
                )
            ]
        )
        answer_generator = RecordingAnswerGenerator()
        event_publisher = RecordingEventPublisher()
        builder = QueryWebAnswerBuilder(
            web_search=web_search,
            build_query_context=BuildQueryContextUseCase(
                embedding_search=EmptySearch(),
                text_search=EmptySearch(),
            ),
            query_answer_assembler=QueryAnswerAssembler(answer_generator),
        )

        result = builder.answer_from_web_search(
            question="쿠버네티스 오퍼레이터가 뭐야?",
            query_rewrite=QueryRewrite(
                original_question="쿠버네티스 오퍼레이터가 뭐야?",
                retrieval_query="kubernetes operator",
            ),
            event_publisher=event_publisher,
            output_language="en",
            response_length="concise",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(web_search.queries, ["kubernetes operator"])
        self.assertEqual(result.retrieval_summary.stop_reason, "web_search_fallback")
        self.assertEqual(result.related_pages[0].page.page_type, "web")
        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["web"])
        self.assertIn("[1]", result.answer.content)
        self.assertIn("web_search_started", event_publisher.events)
        self.assertIn("web_search_executed", event_publisher.events)
        self.assertIn("web_search_answer_generated", event_publisher.events)
        self.assertEqual(result.web_search.requested, True)
        self.assertEqual(result.web_search.executed, True)
        self.assertEqual(result.web_search.result_count, 1)
        self.assertIsNone(result.web_search.error_code)
        executed_event = event_publisher.payloads[event_publisher.events.index("web_search_executed")]
        assert executed_event is not None
        self.assertNotIn("url", executed_event)
        self.assertNotIn("token", executed_event)
        self.assertIsNotNone(answer_generator.last_context)
        assert answer_generator.last_context is not None
        self.assertIn("# Web Fallback Answer Policy", answer_generator.last_context.answer_context)
        self.assertEqual(answer_generator.last_context.output_language, "en")
        self.assertEqual(answer_generator.last_context.response_length, "concise")

    def test_empty_search_reports_execution_without_error(self) -> None:
        telemetry = WebSearchTelemetry()
        builder = QueryWebAnswerBuilder(
            web_search=FakeWebSearch([]),
            build_query_context=BuildQueryContextUseCase(
                embedding_search=EmptySearch(),
                text_search=EmptySearch(),
            ),
            query_answer_assembler=QueryAnswerAssembler(RecordingAnswerGenerator()),
        )

        result = builder.answer_from_web_search(
            question="질문",
            query_rewrite=QueryRewrite(original_question="질문", retrieval_query="query"),
            event_publisher=None,
            web_search_telemetry=telemetry,
        )

        self.assertIsNone(result)
        self.assertEqual(
            (telemetry.requested, telemetry.executed, telemetry.result_count, telemetry.error_code),
            (True, True, 0, None),
        )

    def test_failed_search_reports_stable_error_code_without_provider_detail(self) -> None:
        telemetry = WebSearchTelemetry()
        event_publisher = RecordingEventPublisher()
        builder = QueryWebAnswerBuilder(
            web_search=FailingWebSearch(),
            build_query_context=BuildQueryContextUseCase(
                embedding_search=EmptySearch(),
                text_search=EmptySearch(),
            ),
            query_answer_assembler=QueryAnswerAssembler(RecordingAnswerGenerator()),
        )

        result = builder.answer_from_web_search(
            question="질문",
            query_rewrite=QueryRewrite(original_question="질문", retrieval_query="query"),
            event_publisher=event_publisher,
            web_search_telemetry=telemetry,
        )

        self.assertIsNone(result)
        self.assertEqual(
            (telemetry.requested, telemetry.executed, telemetry.result_count, telemetry.error_code),
            (True, True, 0, "web_search_failed"),
        )
        failed_event = event_publisher.payloads[event_publisher.events.index("web_search_failed")]
        assert failed_event is not None
        self.assertNotIn("provider detail must not be published", str(failed_event))


if __name__ == "__main__":
    unittest.main()
