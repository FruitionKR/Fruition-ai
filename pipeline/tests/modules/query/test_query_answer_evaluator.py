from app.modules.query.infrastructure.query_answer_evaluator import (
    DEFAULT_QUERY_EVALUATOR_PROMPT,
    QueryAnswerEvaluator,
    _normalize_evaluation,
    build_query_answer_evaluator,
)


def test_evaluator_prompt_defines_exclusive_route_order() -> None:
    prompt = DEFAULT_QUERY_EVALUATOR_PROMPT.read_text(encoding="utf-8")

    assert "Choose the first matching route in this exact order" in prompt
    assert prompt.index("2. `web_fallback`") < prompt.index("4. `revise_answer`")
    assert "choose `internal_web_augmented`, never `web_fallback`" in prompt
    assert "not a hallucinated answer that still needs revision" in prompt
    assert "Mandatory answer-safety gate" in prompt
    assert "A limitation statement that the retrieved Wiki evidence does not contain" in prompt
    assert "evidence_relevance` is a reporting metric, not a route threshold" in prompt
    assert "arbitrary numeric cutoff" in prompt
    assert "retrieved evidence is sufficient, but" not in prompt


def test_evaluator_defaults_to_web_search_requests(monkeypatch) -> None:
    monkeypatch.delenv("QUERY_EVALUATOR_MODE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")

    disabled = build_query_answer_evaluator(provider="openai", model="gpt-5-nano")
    evaluator = build_query_answer_evaluator(
        provider="openai",
        model="gpt-5-nano",
        web_search_available=True,
    )

    assert disabled is None
    assert isinstance(evaluator, QueryAnswerEvaluator)


def test_internal_supported_with_actionable_feedback_becomes_revision() -> None:
    evaluation = _normalize_evaluation(
        {
            "route": "internal_supported",
            "evidence_relevance": 0.9,
            "reason": "근거는 충분하지만 인용 문장을 고쳐야 합니다.",
            "feedback": "두 번째 문장의 인용을 직접 근거로 교체하세요.",
            "warnings": [],
        }
    )

    assert evaluation.route == "revise_answer"
    assert evaluation.feedback == "두 번째 문장의 인용을 직접 근거로 교체하세요."


def test_internal_supported_keeps_non_blocking_warnings() -> None:
    evaluation = _normalize_evaluation(
        {
            "route": "internal_supported",
            "evidence_relevance": 0.9,
            "reason": "답변과 근거가 일치합니다.",
            "feedback": "",
            "warnings": ["표현을 더 간결하게 만들 수 있습니다."],
        }
    )

    assert evaluation.route == "internal_supported"
    assert evaluation.warnings == ["표현을 더 간결하게 만들 수 있습니다."]


def test_internal_supported_does_not_use_numeric_relevance_cutoff() -> None:
    evaluation = _normalize_evaluation(
        {
            "route": "internal_supported",
            "evidence_relevance": 0.2,
            "reason": "인접한 내용만 검색되었습니다.",
        },
        web_search_available=False,
        has_internal_evidence=True,
    )

    assert evaluation.route == "internal_supported"
    assert evaluation.evidence_relevance == 0.2


def test_weak_internal_support_preserves_web_fallback() -> None:
    evaluation = _normalize_evaluation(
        {
            "route": "web_fallback",
            "evidence_relevance": 0.2,
            "reason": "인접한 내용만 검색되었습니다.",
        },
        web_search_available=True,
        has_internal_evidence=True,
    )

    assert evaluation.route == "web_fallback"


def test_web_route_becomes_internal_revision_when_web_search_is_unavailable() -> None:
    evaluation = _normalize_evaluation(
        {
            "route": "internal_web_augmented",
            "reason": "내부 문서가 질문의 일부만 설명합니다.",
            "feedback": "",
            "web_query": "외부 구현 방법",
        },
        web_search_available=False,
        has_internal_evidence=True,
    )

    assert evaluation.route == "revise_answer"
    assert "내부 문서가 직접 뒷받침하는 내용만" in evaluation.feedback
    assert evaluation.web_query is None


def test_web_route_becomes_unsupported_without_internal_evidence_or_web_search() -> None:
    evaluation = _normalize_evaluation(
        {
            "route": "web_fallback",
            "reason": "내부 근거가 없습니다.",
            "web_query": "외부 질문",
        },
        web_search_available=False,
        has_internal_evidence=False,
    )

    assert evaluation.route == "unsupported"
    assert evaluation.feedback == ""
    assert evaluation.web_query is None


def test_internal_web_augmented_becomes_web_fallback_without_internal_evidence() -> None:
    evaluation = _normalize_evaluation(
        {
            "route": "internal_web_augmented",
            "reason": "내부 근거가 없습니다.",
            "web_query": "외부 질문",
        },
        web_search_available=True,
        has_internal_evidence=False,
    )

    assert evaluation.route == "web_fallback"
    assert evaluation.web_query == "외부 질문"


def test_internal_web_augmented_becomes_unsupported_without_internal_evidence_or_web() -> None:
    evaluation = _normalize_evaluation(
        {
            "route": "internal_web_augmented",
            "reason": "내부 근거가 없습니다.",
            "web_query": "외부 질문",
        },
        web_search_available=False,
        has_internal_evidence=False,
    )

    assert evaluation.route == "unsupported"
    assert evaluation.web_query is None
