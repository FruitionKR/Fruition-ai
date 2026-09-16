import json
import os
from pathlib import Path
from typing import Any

from app.core.llm_env import (
    api_key_from_env,
    float_env,
    int_env,
    optional_int_env,
    resolve_llm_selection,
)
from app.modules.query.application.ports import QueryEvaluatorPort
from app.modules.query.application.query_evaluator_flow import apply_evidence_sufficiency_boundary
from app.modules.query.domain.entities import GeneratedAnswer, QueryContext, QueryEvaluation
from app.modules.wiki_generation.infrastructure.chat_completions_llm import ChatClientConfig, ChatCompletionsJsonClient


DEFAULT_QUERY_EVALUATOR_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "query_answer_evaluator.system.md"
ALLOWED_ROUTES = {"internal_supported", "revise_answer", "web_fallback", "internal_web_augmented", "unsupported"}


class QueryAnswerEvaluator(QueryEvaluatorPort):
    def __init__(self, client: ChatCompletionsJsonClient, system_prompt: str) -> None:
        self._client = client
        self._system_prompt = system_prompt

    def evaluate(
        self,
        question: str,
        context: QueryContext,
        answer: GeneratedAnswer,
        stop_reason: str,
        web_search_available: bool = False,
    ) -> QueryEvaluation:
        payload = {
            "question": question,
            "resolved_retrieval_question": context.question,
            "answer": answer.content,
            "stop_reason": stop_reason,
            "web_search_available": web_search_available,
            "related_pages": [
                {
                    "id": item.page.id,
                    "page_type": item.page.page_type,
                    "title": item.page.title,
                    "role": item.role,
                    "score": item.score,
                    "summary": item.page.summary,
                }
                for item in context.related_pages[:8]
            ],
            "evidence_snippets": [
                {
                    "rank": snippet.rank,
                    "source_document_id": snippet.source_document_id,
                    "source_block_ids": snippet.source_block_ids,
                    "source_refs": [
                        {
                            "source_document_id": ref.source_document_id,
                            "source_block_id": ref.source_block_id,
                        }
                        for ref in snippet.source_refs
                    ],
                    "text": snippet.text,
                }
                for snippet in context.evidence_snippets[:8]
            ],
        }
        raw = self._client.complete_json(self._system_prompt, json.dumps(payload, ensure_ascii=False, indent=2))
        return _normalize_evaluation(
            raw,
            web_search_available=web_search_available,
            has_internal_evidence=bool(context.evidence_snippets),
        )


def build_query_answer_evaluator(
    *,
    provider: str | None = None,
    model: str | None = None,
    web_search_available: bool = False,
    mode: str | None = None,
) -> QueryEvaluatorPort | None:
    mode = (mode or os.environ.get("QUERY_EVALUATOR_MODE", "web")).strip().lower()
    if mode in {"", "disabled", "off", "none"}:
        return None
    if mode == "web" and not web_search_available:
        return None
    if mode not in {"web", "llm"}:
        return None
    resolved_provider, resolved_model = resolve_llm_selection(provider, model)
    api_key = _api_key(resolved_provider)
    if not api_key:
        return None
    if not resolved_model:
        return None
    prompt_path = Path(os.environ.get("QUERY_EVALUATOR_PROMPT", str(DEFAULT_QUERY_EVALUATOR_PROMPT)))
    system_prompt = prompt_path.read_text(encoding="utf-8")
    return QueryAnswerEvaluator(
        ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=api_key,
                model=resolved_model,
                temperature=None,
                timeout_seconds=_int_env("QUERY_EVALUATOR_TIMEOUT_SECONDS", 180),
                max_tokens=_optional_int_env("QUERY_EVALUATOR_MAX_TOKENS"),
                json_mode=True,
                provider=resolved_provider,
            )
        ),
        system_prompt=system_prompt,
    )


def _normalize_evaluation(
    value: dict[str, Any],
    *,
    web_search_available: bool = True,
    has_internal_evidence: bool = True,
) -> QueryEvaluation:
    route = str(value.get("route") or "internal_supported").strip()
    if route not in ALLOWED_ROUTES:
        route = "internal_supported"
    feedback = str(value.get("feedback") or "").strip()
    if route == "internal_supported" and feedback:
        route = "revise_answer"
    web_query = _optional_text(value.get("web_query"))
    if not web_search_available and route in {"web_fallback", "internal_web_augmented"}:
        web_query = None
        if has_internal_evidence:
            route = "revise_answer"
            feedback = (
                "웹 검색을 사용할 수 없습니다. 현재 내부 문서가 직접 뒷받침하는 내용만 먼저 답하고, "
                "요청 중 확인할 수 없는 부분은 내부 문서에서 근거를 찾지 못했다고 명시하세요."
            )
        else:
            route = "unsupported"
            feedback = ""
    evaluation = QueryEvaluation(
        route=route,
        evidence_relevance=_bounded_float(value.get("evidence_relevance"), 0.0),
        citation_evidence_alignment=_optional_bounded_float(value.get("citation_evidence_alignment")),
        unsupported_refusal_accuracy=_optional_bounded_float(value.get("unsupported_refusal_accuracy")),
        reason=str(value.get("reason") or ""),
        feedback=feedback,
        web_query=web_query,
        warnings=[str(item).strip() for item in value.get("warnings", []) if str(item).strip()],
    )
    return apply_evidence_sufficiency_boundary(
        evaluation,
        has_internal_evidence=has_internal_evidence,
        web_search_available=web_search_available,
    )


def _api_key(provider: str | None = None) -> str | None:
    return api_key_from_env(
        provider=provider,
    )


def _bounded_float(value: object, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _optional_bounded_float(value: object) -> float | None:
    if value is None:
        return None
    return _bounded_float(value, 0.0)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _float_env(name: str, default: float) -> float:
    return float_env(name, default)


def _int_env(name: str, default: int) -> int:
    return int_env(name, default)


def _optional_int_env(name: str) -> int | None:
    return optional_int_env(name)
