import unittest

from app.modules.query.application.query_answer_assembler import QueryAnswerAssembler
from app.modules.query.domain.entities import EvidenceSnippet, GeneratedAnswer, GraphContext, QueryContext, QueryEvaluation
from app.modules.query.infrastructure.query_evaluator_graph import LangGraphQueryEvaluatorGraph


class SequencedAnswerGenerator:
    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.contexts: list[QueryContext] = []

    def generate_answer(self, context: QueryContext) -> GeneratedAnswer:
        self.contexts.append(context)
        index = min(len(self.contexts) - 1, len(self.contents) - 1)
        return GeneratedAnswer(content=self.contents[index])


class FakeQueryEvaluator:
    def __init__(self, evaluations: list[QueryEvaluation]) -> None:
        self.evaluations = evaluations
        self.calls: list[GeneratedAnswer] = []
        self.web_search_availability: list[bool] = []

    def evaluate(
        self,
        question: str,
        context: QueryContext,
        answer: GeneratedAnswer,
        stop_reason: str,
        web_search_available: bool = False,
    ) -> QueryEvaluation:
        self.calls.append(answer)
        self.web_search_availability.append(web_search_available)
        index = min(len(self.calls) - 1, len(self.evaluations) - 1)
        return self.evaluations[index]


class FailingEventPublisher:
    def publish(self, stage: str, message: str, data: dict[str, object] | None = None) -> None:
        raise RuntimeError("progress publish failed")


class QueryEvaluatorGraphTest(unittest.TestCase):
    def test_progress_publish_failure_does_not_fail_the_run(self) -> None:
        """진행 이벤트는 화면 피드백용이라, 발행이 실패해도 답변은 그대로 나와야 한다."""
        answer_generator = SequencedAnswerGenerator(["내부 답변입니다."])
        query_evaluator = FakeQueryEvaluator([QueryEvaluation(route="internal_supported")])
        graph = LangGraphQueryEvaluatorGraph(
            query_answer_assembler=QueryAnswerAssembler(answer_generator),
            query_evaluator=query_evaluator,
            web_search_available=False,
            max_attempts=1,
        )
        context = QueryContext(
            question="질문",
            graph_context=GraphContext(),
            traversal_paths=[],
            related_pages=[],
            evidence_snippets=[],
            answer_context="질문",
        )

        answer, _, _, evaluation = graph.run(
            question="질문",
            query_context=context,
            stop_reason="no_relevant_seed",
            event_publisher=FailingEventPublisher(),
        )

        self.assertEqual(answer.content, "내부 답변입니다.")
        self.assertEqual(evaluation.route if evaluation else None, "internal_supported")

    def test_request_can_disable_web_fallback_for_evaluator(self) -> None:
        answer_generator = SequencedAnswerGenerator(["내부 답변입니다."])
        query_evaluator = FakeQueryEvaluator([QueryEvaluation(route="internal_supported")])
        graph = LangGraphQueryEvaluatorGraph(
            query_answer_assembler=QueryAnswerAssembler(answer_generator),
            query_evaluator=query_evaluator,
            web_search_available=True,
            max_attempts=1,
        )
        context = QueryContext(
            question="질문",
            graph_context=GraphContext(),
            traversal_paths=[],
            related_pages=[],
            evidence_snippets=[],
            answer_context="질문",
            allow_web_search=False,
        )

        graph.run(
            question="질문",
            query_context=context,
            stop_reason="no_relevant_seed",
            event_publisher=None,
        )

        self.assertEqual(query_evaluator.web_search_availability, [False])

    def test_langgraph_query_evaluator_retries_with_feedback(self) -> None:
        answer_generator = SequencedAnswerGenerator(
            [
                "초안 답변입니다. [1]",
                "피드백을 반영한 답변입니다. [1]",
            ]
        )
        query_evaluator = FakeQueryEvaluator(
            [
                QueryEvaluation(
                    route="revise_answer",
                    evidence_relevance=0.2,
                    reason="근거 반영이 부족합니다.",
                    feedback="근거 문장을 직접 반영하세요.",
                ),
                QueryEvaluation(
                    route="internal_supported",
                    evidence_relevance=0.95,
                    reason="내부 근거로 충분합니다.",
                ),
            ]
        )
        graph = LangGraphQueryEvaluatorGraph(
            query_answer_assembler=QueryAnswerAssembler(answer_generator),
            query_evaluator=query_evaluator,
            web_search_available=False,
            max_attempts=2,
        )
        context = QueryContext(
            question="LangSmith evaluator graph는 어떻게 확인하나요?",
            graph_context=GraphContext(),
            traversal_paths=[],
            related_pages=[],
            evidence_snippets=[
                EvidenceSnippet(
                    rank=1,
                    source_document_id="doc",
                    source_block_ids=["B0001"],
                    text="LangSmith tracing과 query evaluator mode를 켜면 evaluator graph 실행이 기록됩니다.",
                )
            ],
            answer_context="[1] LangSmith tracing과 query evaluator mode를 켜면 evaluator graph 실행이 기록됩니다.",
        )

        answer, evidence_snippets, evaluated_context, evaluation = graph.run(
            question="LangSmith evaluator graph는 어떻게 확인하나요?",
            query_context=context,
            stop_reason="answer_context_selected",
            event_publisher=None,
        )

        self.assertEqual(len(query_evaluator.calls), 2)
        self.assertEqual(len(answer_generator.contexts), 2)
        self.assertIn("근거 문장을 직접 반영하세요.", answer_generator.contexts[1].answer_context)
        self.assertIn("피드백을 반영한 답변입니다.", answer.content)
        self.assertEqual(evidence_snippets[0].source_block_ids, ["B0001"])
        self.assertEqual(evaluated_context.question, context.question)
        self.assertEqual(evaluation.route if evaluation else None, "internal_supported")

    def test_does_not_retry_terminal_route_even_with_feedback(self) -> None:
        answer_generator = SequencedAnswerGenerator(["초안 답변입니다. [1]"])
        query_evaluator = FakeQueryEvaluator(
            [
                QueryEvaluation(
                    route="unsupported",
                    evidence_relevance=0.0,
                    reason="현재 근거로 답할 수 없습니다.",
                    feedback="외부 근거를 찾으세요.",
                )
            ]
        )
        graph = LangGraphQueryEvaluatorGraph(
            query_answer_assembler=QueryAnswerAssembler(answer_generator),
            query_evaluator=query_evaluator,
            web_search_available=False,
            max_attempts=2,
        )
        context = QueryContext(
            question="비공개 정보를 알려줘",
            graph_context=GraphContext(),
            traversal_paths=[],
            related_pages=[],
            evidence_snippets=[],
            answer_context="",
        )

        _, _, _, evaluation = graph.run(
            question=context.question,
            query_context=context,
            stop_reason="no_relevant_seed",
            event_publisher=None,
        )

        self.assertEqual(len(query_evaluator.calls), 1)
        self.assertEqual(len(answer_generator.contexts), 1)
        self.assertEqual(evaluation.route if evaluation else None, "unsupported")


if __name__ == "__main__":
    unittest.main()
