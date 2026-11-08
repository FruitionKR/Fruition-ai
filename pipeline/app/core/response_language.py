"""요청자 화면 언어(response_language)에 맞춰 사용자에게 보이는 생성 결과의 언어를 정한다.

document-svc가 command·요청에 `response_language`("ko"|"en")를 싣는다. 값이 없으면 지금처럼 동작한다(한국어 고정
문구, 기존 프롬프트 그대로). 위키 편입처럼 워크스페이스가 함께 쓰는 자산과 문서 본문 편집에는 쓰지 않는다.

실행 단위(task worker command, agent worker job) 진입점에서 `response_language_scope`로 값을 정하면, 그 안의
프롬프트 조립·고정 문구가 `current_response_language()`로 읽는다. contextvars라 copy_context로 넘긴 스레드에도 이어진다.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

ResponseLanguage = Literal["ko", "en"]

_language: ContextVar[ResponseLanguage | None] = ContextVar("response_language", default=None)

# 한국어 고정 문구 → 영어. `register_en`으로 모듈이 상수를 정의하면서 등록한다.
_EN_TEXTS: dict[str, str] = {}


def response_language_from(value: object) -> ResponseLanguage | None:
    """command 값을 언어로 바꾼다. 없거나 지원하지 않는 값은 None(기존 동작)이다."""
    if isinstance(value, str) and value.strip().lower() in {"ko", "en"}:
        return value.strip().lower()  # type: ignore[return-value]
    return None


@contextmanager
def response_language_scope(value: object) -> Iterator[ResponseLanguage | None]:
    token = _language.set(response_language_from(value))
    try:
        yield _language.get()
    finally:
        _language.reset(token)


def current_response_language() -> ResponseLanguage | None:
    return _language.get()


def is_english() -> bool:
    return _language.get() == "en"


def localized(ko: str, en: str) -> str:
    """요청 언어가 영어면 en, 아니면(한국어·미지정) ko를 돌려준다. 값을 채운 f-string 문구에 쓴다."""
    return en if is_english() else ko


def register_en(ko: str, en: str) -> str:
    """한국어 고정 문구의 영어를 등록하고 한국어를 그대로 돌려준다. 상수 비교는 한국어 값으로 계속 한다."""
    _EN_TEXTS[ko] = en
    return ko


def localize_text(text: str | None) -> str | None:
    """등록한 한국어 고정 문구를 영어로 바꾼다. 영어 요청이 아니면 그대로 둔다.

    결과 메시지는 고정 문구와 안내가 이어 붙은 값이라 문장 단위로 바꾼다. 긴 문구를 먼저 바꿔
    짧은 문구가 긴 문구의 일부를 먼저 바꾸지 않게 한다. 등록하지 않은 생성 문장은 건드리지 않는다.
    """
    if not text or not is_english():
        return text
    for ko in sorted(_EN_TEXTS, key=len, reverse=True):
        if ko in text:
            text = text.replace(ko, _EN_TEXTS[ko])
    return text


# 사용자에게 보이는 문장에 한국어를 고정한 프롬프트 지시 → 영어 지시. 끝에 덧붙인 언어 지시만으로는 모델이 앞의
# 고정 지시를 따르는 경우가 있어, 영어 요청이면 고정 지시 자체를 바꾼다. 문서 본문 언어 지시는 넣지 않는다
# (예: markdown_create의 "Use Korean unless ..."는 본문 규칙이라 그대로 둔다).
_EN_PROMPT_RULES: dict[str, str] = {
    # task_clarification.system.md
    "Ask exactly one question in Korean, in the polite `~요` style,": "Ask exactly one question in English, in a polite tone,",
    # agent_folder_plan.system.md
    "a brief Korean clarification question": "a brief English clarification question",
    "short Korean sentences": "short English sentences",
    "a non-empty brief Korean string": "a non-empty brief English string",
    "a brief user-facing Korean reason": "a brief user-facing English reason",
    # markdown_edit·markdown_create·markdown_source_edit.system.md의 summary
    "Write summary in Korean.": "Write summary in English.",
    "Korean one-sentence summary": "English one-sentence summary",
}


def _with_english_prompt_rules(system_prompt: str) -> str:
    for ko, en in _EN_PROMPT_RULES.items():
        system_prompt = system_prompt.replace(ko, en)
    return system_prompt


def response_language_rule() -> str | None:
    """프롬프트에 덧붙일 응답 언어 지시. 요청 언어가 없으면 None이다."""
    language = _language.get()
    if language == "en":
        return "Write every user-facing sentence in English, regardless of the language of the input or evidence."
    if language == "ko":
        return "Write every user-facing sentence in Korean, regardless of the language of the input or evidence."
    return None


def with_response_language(system_prompt: str) -> str:
    """요청 언어가 있으면 응답 언어 지시를 system prompt 끝에 붙인다. 없으면 그대로 돌려준다(하위호환)."""
    rule = response_language_rule()
    if rule is None:
        return system_prompt
    if is_english():
        system_prompt = _with_english_prompt_rules(system_prompt)
    return (
        f"{system_prompt.rstrip()}\n\n# Response Language\n- {rule}\n"
        "- Keep quotations, code, identifiers, JSON keys, and proper nouns in their original form.\n"
    )


def with_summary_language(system_prompt: str) -> str:
    """편집·새 문서 결과의 summary만 요청 언어로 쓰게 한다. 문서 본문 언어는 바꾸지 않는다.

    문서는 워크스페이스가 함께 쓰는 자산이라 본문 언어는 기존 규칙(문서·지시 언어)을 따른다. summary는 요청자에게
    보이는 안내라 화면 언어를 따른다. 영어 요청이 아니면 그대로 돌려준다(기존 프롬프트가 summary를 한국어로 지시한다).
    """
    if not is_english():
        return system_prompt
    return (
        f"{_with_english_prompt_rules(system_prompt).rstrip()}\n\n# Summary Language\n"
        "- Write the `summary` field in English. This overrides any earlier instruction about the summary language.\n"
        "- Do not change the language of the document content (Markdown, title, body) because of this rule.\n"
    )
