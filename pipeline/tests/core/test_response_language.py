from contextvars import copy_context

from app.core.response_language import (
    current_response_language,
    localize_text,
    localized,
    register_en,
    response_language_from,
    response_language_scope,
    with_response_language,
    with_summary_language,
)

KO = register_en("테스트 고정 문구입니다.", "This is a fixed test sentence.")


def test_only_ko_and_en_are_supported_languages() -> None:
    assert response_language_from("en") == "en"
    assert response_language_from(" KO ") == "ko"
    assert response_language_from("fr") is None
    assert response_language_from(None) is None
    assert response_language_from(1) is None


def test_without_language_prompts_and_texts_stay_unchanged() -> None:
    assert current_response_language() is None
    assert with_response_language("prompt") == "prompt"
    assert with_summary_language("prompt") == "prompt"
    assert localized("한국어", "English") == "한국어"
    assert localize_text(f"앞 {KO}") == f"앞 {KO}"


def test_english_scope_translates_registered_sentences_and_adds_prompt_rule() -> None:
    with response_language_scope("en"):
        assert localized("한국어", "English") == "English"
        # 등록한 고정 문구만 바꾸고 나머지 문장은 그대로 둔다.
        assert localize_text(f"생성된 답변\n\n{KO}") == "생성된 답변\n\nThis is a fixed test sentence."
        assert "Write every user-facing sentence in English" in with_response_language("prompt")
        assert "Write the `summary` field in English" in with_summary_language("prompt")
    assert current_response_language() is None


def test_korean_scope_keeps_korean_texts_but_instructs_korean_answers() -> None:
    with response_language_scope("ko"):
        assert localized("한국어", "English") == "한국어"
        assert localize_text(KO) == KO
        assert "in Korean" in with_response_language("prompt")
        assert with_summary_language("prompt") == "prompt"


def test_scope_value_follows_copied_context_into_threads() -> None:
    with response_language_scope("en"):
        context = copy_context()
    assert context.run(current_response_language) == "en"
