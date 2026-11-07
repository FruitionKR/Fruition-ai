from contextvars import copy_context
from pathlib import Path

import pytest

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


PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
# 사용자에게 보이는 문장(되묻기·계획 안내·편집 summary)을 쓰는 프롬프트와 그 프롬프트를 감싸는 함수.
USER_FACING_PROMPTS = (
    ("task_clarification.system.md", with_response_language),
    ("agent_folder_plan.system.md", with_response_language),
    ("markdown_edit.system.md", with_summary_language),
    ("markdown_source_edit.system.md", with_summary_language),
)


@pytest.mark.parametrize(("name", "wrap"), USER_FACING_PROMPTS)
def test_english_request_leaves_no_fixed_korean_instruction(name: str, wrap) -> None:
    # 끝에 붙인 영어 지시만으로는 모델이 앞의 고정 한국어 지시를 따르기도 한다. 고정 지시 자체가 남지 않아야 한다.
    prompt = (PROMPTS / name).read_text(encoding="utf-8")
    assert "Korean" in prompt
    with response_language_scope("en"):
        assert "Korean" not in wrap(prompt)


def test_english_create_prompt_changes_summary_but_keeps_document_language_rule() -> None:
    # 새 문서 본문 언어는 공유 자산이라 요청자 화면 언어로 바꾸지 않는다(ADR-0045).
    prompt = (PROMPTS / "markdown_create.system.md").read_text(encoding="utf-8")
    with response_language_scope("en"):
        localized_prompt = with_summary_language(prompt)
    assert "Korean one-sentence summary" not in localized_prompt
    assert "English one-sentence summary" in localized_prompt
    assert "Use Korean unless the user explicitly asks for another language." in localized_prompt


@pytest.mark.parametrize(("name", "wrap"), USER_FACING_PROMPTS)
def test_korean_or_missing_language_keeps_prompt_text(name: str, wrap) -> None:
    prompt = (PROMPTS / name).read_text(encoding="utf-8")
    assert wrap(prompt) == prompt
    with response_language_scope("ko"):
        assert wrap(prompt).startswith(prompt.rstrip())
