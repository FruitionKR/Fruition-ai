from dataclasses import replace
from typing import TypedDict

from app.modules.query.application.ports import QueryEvaluatorPort, QueryEventPublisherPort
from app.modules.query.application.query_answer_assembler import QueryAnswerAssembler
from app.modules.query.application.query_event import publish_query_event
from app.modules.query.domain.entities import EvidenceSnippet, GeneratedAnswer, QueryContext, QueryEvaluation


class QueryEvaluationGraphState(TypedDict):
    attempt: int
    answer: GeneratedAnswer
    evidence_snippets: list[EvidenceSnippet]
    evaluated_context: QueryContext
    evaluation: QueryEvaluation | None


class QueryEvaluatorLoop:
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
        state = initial_query_evaluation_state(query_context)
        while True:
            state = generate_answer_step(
                state,
                query_context=query_context,
                query_answer_assembler=self._query_answer_assembler,
                event_publisher=event_publisher,
            )
            state = evaluate_answer_step(
                state,
                question=question,
                stop_reason=stop_reason,
                query_evaluator=self._query_evaluator,
                web_search_available=(
                    self._web_search_available
                    and query_context.allow_web_search is not False
                ),
                event_publisher=event_publisher,
            )
            if route_after_evaluation(state, self._max_attempts) != "retry":
                return query_evaluation_result(state)
            state = prepare_retry_step(state)


def initial_query_evaluation_state(query_context: QueryContext) -> QueryEvaluationGraphState:
    return {
        "attempt": 1,
        "answer": GeneratedAnswer(content=""),
        "evidence_snippets": [],
        "evaluated_context": query_context,
        "evaluation": None,
    }


def generate_answer_step(
    state: QueryEvaluationGraphState,
    *,
    query_context: QueryContext,
    query_answer_assembler: QueryAnswerAssembler,
    event_publisher: QueryEventPublisherPort | None,
) -> QueryEvaluationGraphState:
    attempt = int(state.get("attempt", 1))
    context = query_context_with_evaluator_feedback(query_context, state.get("evaluation"), attempt)
    answer, evidence_snippets = query_answer_assembler.generate_supported_answer(context)
    evaluated_context = replace(context, evidence_snippets=evidence_snippets)
    publish_query_event(
        event_publisher,
        "answer_generated",
        "답변 생성을 완료했습니다.",
        {"answer_chars": len(answer.content), "attempt": attempt},
    )
    return {
        **state,
        "answer": answer,
        "evidence_snippets": evidence_snippets,
        "evaluated_context": evaluated_context,
    }


def evaluate_answer_step(
    state: QueryEvaluationGraphState,
    *,
    question: str,
    stop_reason: str,
    query_evaluator: QueryEvaluatorPort | None,
    web_search_available: bool,
    event_publisher: QueryEventPublisherPort | None,
) -> QueryEvaluationGraphState:
    if query_evaluator is None:
        return {**state, "evaluation": None}
    answer = state["answer"]
    evaluated_context = state["evaluated_context"]
    try:
        evaluation = query_evaluator.evaluate(
            question,
            evaluated_context,
            answer,
            stop_reason,
            web_search_available=web_search_available,
        )
    except Exception as exc:
        publish_query_event(
            event_publisher, "query_evaluation_failed", "Query evaluator 실행에 실패했습니다.", {"error": str(exc)}
        )
        return {**state, "evaluation": None}
    publish_query_event(
        event_publisher,
        "query_evaluated",
        "검색 근거와 질문의 정합성을 평가했습니다.",
        {
            "route": evaluation.route,
            "evidence_relevance": round(evaluation.evidence_relevance, 4),
            "reason": evaluation.reason,
            "web_query": evaluation.web_query,
        },
    )
    return {**state, "evaluation": evaluation}


def apply_evidence_sufficiency_boundary(
    evaluation: QueryEvaluation,
    *,
    has_internal_evidence: bool,
    web_search_available: bool,
) -> QueryEvaluation:
    if not web_search_available and evaluation.route in {
        "web_fallback",
        "internal_web_augmented",
    }:
        if has_internal_evidence:
            return replace(
                evaluation,
                route="revise_answer",
                feedback=(
                    "웹 검색을 사용할 수 없습니다. 현재 내부 문서가 직접 뒷받침하는 내용만 먼저 답하고, "
                    "요청 중 확인할 수 없는 부분은 내부 문서에서 근거를 찾지 못했다고 명시하세요."
                ),
                web_query=None,
            )
        return replace(evaluation, route="unsupported", feedback="", web_query=None)
    if evaluation.route == "internal_web_augmented" and not has_internal_evidence:
        return replace(evaluation, route="web_fallback", feedback="")
    if evaluation.route != "internal_supported":
        return evaluation
    if has_internal_evidence:
        return evaluation
    return replace(
        evaluation,
        route="web_fallback" if web_search_available else "unsupported",
        feedback="",
        web_query=evaluation.web_query if web_search_available else None,
    )


def route_after_evaluation(state: QueryEvaluationGraphState, max_attempts: int) -> str:
    evaluation = state.get("evaluation")
    if evaluation is None or evaluation.route == "internal_supported":
        return "accepted"
    if evaluation.route != "revise_answer":
        return "finished"
    if int(state.get("attempt", 1)) >= max(1, max_attempts):
        return "finished"
    if not evaluation.feedback.strip():
        return "finished"
    return "retry"


def prepare_retry_step(state: QueryEvaluationGraphState) -> QueryEvaluationGraphState:
    return {**state, "attempt": int(state.get("attempt", 1)) + 1}


def query_evaluation_result(
    state: QueryEvaluationGraphState,
) -> tuple[GeneratedAnswer, list[EvidenceSnippet], QueryContext, QueryEvaluation | None]:
    return (
        state["answer"],
        state["evidence_snippets"],
        state["evaluated_context"],
        state.get("evaluation"),
    )


def query_context_with_evaluator_feedback(
    query_context: QueryContext,
    evaluation: QueryEvaluation | None,
    attempt: int,
) -> QueryContext:
    if attempt <= 1 or evaluation is None or not evaluation.feedback.strip():
        return query_context
    feedback_block = (
        "\n\n# Evaluator Feedback For Retry\n"
        "이전 답변은 evaluator 기준을 통과하지 못했습니다. 아래 피드백을 반영해 같은 근거 안에서 답변을 다시 작성하세요.\n"
        f"- route: {evaluation.route}\n"
        f"- reason: {evaluation.reason}\n"
        f"- feedback: {evaluation.feedback}\n"
    )
    return replace(query_context, answer_context=query_context.answer_context + feedback_block)
