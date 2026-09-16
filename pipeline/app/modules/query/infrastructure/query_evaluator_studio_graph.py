from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.core.langsmith_tracing import disable_unconfigured_langsmith_tracing
from app.modules.query.domain.entities import EvidenceSnippet, GeneratedAnswer, GraphContext, QueryContext, QueryEvaluation, RetrievedPage, SourceReference, WikiPage
from app.modules.query.infrastructure.query_answer_evaluator import build_query_answer_evaluator


disable_unconfigured_langsmith_tracing()


class QueryEvaluatorStudioState(TypedDict, total=False):
    question: str
    resolved_retrieval_question: str
    answer: str
    stop_reason: str
    web_search_available: bool
    related_pages: list[dict[str, Any]]
    evidence_snippets: list[dict[str, Any]]
    answer_context: str
    attempt: int
    max_attempts: int
    evaluation: dict[str, Any] | None
    feedback: str


def generate_answer(state: QueryEvaluatorStudioState) -> QueryEvaluatorStudioState:
    attempt = int(state.get("attempt") or 1)
    answer = str(state.get("answer") or "").strip()
    if not answer:
        answer = "제공된 evidence를 기준으로 답변을 생성하지 못했습니다."
    if attempt > 1 and state.get("feedback"):
        answer = f"{answer}\n\nEvaluator feedback 반영 필요: {state['feedback']}"
    return {**state, "attempt": attempt, "answer": answer}


def evaluate_answer(state: QueryEvaluatorStudioState) -> QueryEvaluatorStudioState:
    web_search_available = bool(state.get("web_search_available", False))
    evaluator = build_query_answer_evaluator(
        provider="openai",
        model="gpt-5-nano",
        web_search_available=web_search_available,
    )
    if evaluator is None:
        return {
            **state,
            "evaluation": {
                "route": "internal_supported",
                "evidence_relevance": 0.0,
                "reason": "QUERY_EVALUATOR_MODE 또는 API key가 설정되지 않아 evaluator를 실행하지 않았습니다.",
                "feedback": "",
                "web_query": None,
            },
            "feedback": "",
        }

    evaluation = evaluator.evaluate(
        str(state.get("question") or ""),
        _query_context_from_state(state),
        GeneratedAnswer(content=str(state.get("answer") or "")),
        str(state.get("stop_reason") or "answer_context_selected"),
        web_search_available=web_search_available,
    )
    return {**state, "evaluation": _evaluation_dict(evaluation), "feedback": evaluation.feedback}


def route_after_evaluation(state: QueryEvaluatorStudioState) -> str:
    evaluation = state.get("evaluation") or {}
    route = str(evaluation.get("route") or "internal_supported")
    if route == "internal_supported":
        return "accepted"
    if route != "revise_answer":
        return "finished"
    if int(state.get("attempt") or 1) >= int(state.get("max_attempts") or 1):
        return "finished"
    if not str(evaluation.get("feedback") or "").strip():
        return "finished"
    return "retry"


def prepare_retry(state: QueryEvaluatorStudioState) -> QueryEvaluatorStudioState:
    return {**state, "attempt": int(state.get("attempt") or 1) + 1}


def build_graph() -> StateGraph:
    graph = StateGraph(QueryEvaluatorStudioState)
    graph.add_node("generate_answer", generate_answer)
    graph.add_node("evaluate_answer", evaluate_answer)
    graph.add_node("prepare_retry", prepare_retry)
    graph.set_entry_point("generate_answer")
    graph.add_edge("generate_answer", "evaluate_answer")
    graph.add_conditional_edges(
        "evaluate_answer",
        route_after_evaluation,
        {
            "accepted": END,
            "finished": END,
            "retry": "prepare_retry",
        },
    )
    graph.add_edge("prepare_retry", "generate_answer")
    return graph


def _query_context_from_state(state: QueryEvaluatorStudioState) -> QueryContext:
    return QueryContext(
        question=str(state.get("resolved_retrieval_question") or state.get("question") or ""),
        graph_context=GraphContext(),
        traversal_paths=[],
        related_pages=[_retrieved_page(item) for item in state.get("related_pages", [])],
        evidence_snippets=[_evidence_snippet(item, index) for index, item in enumerate(state.get("evidence_snippets", []), start=1)],
        answer_context=str(state.get("answer_context") or ""),
    )


def _retrieved_page(item: dict[str, Any]) -> RetrievedPage:
    page = WikiPage(
        id=str(item.get("id") or item.get("page_id") or "studio-page"),
        page_type=str(item.get("page_type") or "concept"),
        title=str(item.get("title") or "Studio Page"),
        slug=str(item.get("slug") or item.get("id") or "studio-page"),
        summary=str(item.get("summary") or ""),
    )
    return RetrievedPage(
        page=page,
        score=_float(item.get("score"), 0.0),
        role=str(item.get("role") or page.page_type),
    )


def _evidence_snippet(item: dict[str, Any], index: int) -> EvidenceSnippet:
    source_refs = [
        SourceReference(
            source_document_id=str(ref.get("source_document_id") or ""),
            source_block_id=str(ref.get("source_block_id") or ""),
        )
        for ref in item.get("source_refs", [])
        if isinstance(ref, dict)
    ]
    return EvidenceSnippet(
        rank=int(item.get("rank") or index),
        source_document_id=str(item.get("source_document_id") or "studio-document"),
        source_block_ids=[str(value) for value in item.get("source_block_ids", [])],
        source_refs=[
            ref
            for ref in source_refs
            if ref.source_document_id and ref.source_block_id
        ],
        text=str(item.get("text") or ""),
    )


def _evaluation_dict(evaluation: QueryEvaluation) -> dict[str, Any]:
    return {
        "route": evaluation.route,
        "evidence_relevance": evaluation.evidence_relevance,
        "citation_evidence_alignment": evaluation.citation_evidence_alignment,
        "unsupported_refusal_accuracy": evaluation.unsupported_refusal_accuracy,
        "reason": evaluation.reason,
        "feedback": evaluation.feedback,
        "web_query": evaluation.web_query,
        "warnings": evaluation.warnings,
    }


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


graph = build_graph().compile()
