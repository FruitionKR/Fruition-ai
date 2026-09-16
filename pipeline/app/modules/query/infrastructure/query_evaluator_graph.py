from langgraph.graph import END, StateGraph

from app.core.langsmith_tracing import configured_langsmith_tracing
from app.modules.query.application.ports import QueryEvaluatorPort, QueryEventPublisherPort
from app.modules.query.application.query_answer_assembler import QueryAnswerAssembler
from app.modules.query.application.query_evaluator_flow import (
    QueryEvaluationGraphState,
    evaluate_answer_step,
    generate_answer_step,
    initial_query_evaluation_state,
    prepare_retry_step,
    query_evaluation_result,
    route_after_evaluation,
)
from app.modules.query.domain.entities import EvidenceSnippet, GeneratedAnswer, QueryContext, QueryEvaluation


class LangGraphQueryEvaluatorGraph:
    def __init__(
        self,
        *,
        query_answer_assembler: QueryAnswerAssembler,
        query_evaluator: QueryEvaluatorPort | None,
        web_search_available: bool,
        max_attempts: int,
    ) -> None:
        self._query_answer_assembler = query_answer_assembler
        self._query_evaluator = query_evaluator
        self._web_search_available = web_search_available
        self._max_attempts = max(1, max_attempts)

    def run(
        self,
        *,
        question: str,
        query_context: QueryContext,
        stop_reason: str,
        event_publisher: QueryEventPublisherPort | None,
    ) -> tuple[GeneratedAnswer, list[EvidenceSnippet], QueryContext, QueryEvaluation | None]:
        graph = build_query_evaluator_graph(
            query_answer_assembler=self._query_answer_assembler,
            query_evaluator=self._query_evaluator,
            web_search_available=(
                self._web_search_available
                and query_context.allow_web_search is not False
            ),
            max_attempts=self._max_attempts,
            question=question,
            query_context=query_context,
            stop_reason=stop_reason,
            event_publisher=event_publisher,
        )
        with configured_langsmith_tracing():
            result = graph.compile().invoke(initial_query_evaluation_state(query_context))
        return query_evaluation_result(result)


def build_query_evaluator_graph(
    *,
    query_answer_assembler: QueryAnswerAssembler,
    query_evaluator: QueryEvaluatorPort | None,
    web_search_available: bool,
    max_attempts: int,
    question: str,
    query_context: QueryContext,
    stop_reason: str,
    event_publisher: QueryEventPublisherPort | None,
) -> StateGraph:
    graph = StateGraph(QueryEvaluationGraphState)

    def generate_answer(state: QueryEvaluationGraphState) -> QueryEvaluationGraphState:
        return generate_answer_step(
            state,
            query_context=query_context,
            query_answer_assembler=query_answer_assembler,
            event_publisher=event_publisher,
        )

    def evaluate_answer(state: QueryEvaluationGraphState) -> QueryEvaluationGraphState:
        return evaluate_answer_step(
            state,
            question=question,
            stop_reason=stop_reason,
            query_evaluator=query_evaluator,
            web_search_available=web_search_available,
            event_publisher=event_publisher,
        )

    graph.add_node("generate_answer", generate_answer)
    graph.add_node("evaluate_answer", evaluate_answer)
    graph.add_node("prepare_retry", prepare_retry_step)
    graph.set_entry_point("generate_answer")
    graph.add_edge("generate_answer", "evaluate_answer")
    graph.add_conditional_edges(
        "evaluate_answer",
        lambda state: route_after_evaluation(state, max_attempts),
        {
            "accepted": END,
            "finished": END,
            "retry": "prepare_retry",
        },
    )
    graph.add_edge("prepare_retry", "generate_answer")
    return graph
