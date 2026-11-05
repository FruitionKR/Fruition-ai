"""편집 세부 분류를 Jev가 고른다. 쓸 수 없으면 기존 LLM 분류로 처리한다.

Jev는 질문마다 선택지 하나만 고른다. 형식 조합은 2,048개라 선택지 한도(255)를 넘고, 형식마다 예/아니오로 물으면
다른 형식과 견주지 못해 "수식 블록"에 코드 블록까지 고르는 일이 반복됐다. 그래서 주 형식과 둘째 형식을 각각 하나씩 고른다.
선택지 설명에는 LLM 프롬프트의 정의를 그대로 쓴다. 질문을 한 요청으로 보내고, 판정 규칙은 state에 한 번만 둔다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from typing import Any

from app.core.jev_client import JEV_EDIT_DETAIL_ENABLED_ENV, JevClient, JevUnavailable, build_jev_client
from app.modules.markdown_edit.domain.entities import EDIT_FORMATS, MarkdownEditDetail, MarkdownEditRequest
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_edit_detail_classifier import (
    DEFAULT_MARKDOWN_EDIT_DETAIL_PROMPT,
    ChatCompletionsMarkdownEditDetailClassifier,
    edit_detail_payload,
)

FORMAT_NONE = "none"
FIRST_FORMAT_QUESTION = "first_form"
SECOND_FORMAT_QUESTION = "second_form"
FORMAT_DEFINITIONS = {
    "meeting_notes": "rewrite the target into meeting notes with discussion, decision, and next-step sections",
    "table": "present the result as a Markdown table",
    "numbered_list": "present items as an ordered, numbered list",
    "blockquote": "put text in a blockquote",
    "heading": "add or change a Markdown heading",
    "bold": "apply bold inline style to some text",
    "italic": "apply italic inline style to some text",
    "strikethrough": "apply strikethrough inline style to some text",
    "display_math": "write an equation as display math",
    "mermaid": "draw a Mermaid diagram",
    "code_block": "put content in a fenced code block",
}
ADDITION_ONLY_QUESTION = "addition_only"
ADDITION_ONLY_CRITERIA = {
    "yes": "The request only adds new content and every existing line must stay unchanged, "
    "such as filling in content under existing items or appending a sentence",
    "no": "The request also rewrites, reorders, restyles, shortens, translates, converts, or removes existing content",
}
STRUCTURE_CHANGE_QUESTION = "structure_change"
STRUCTURE_CHANGE_CRITERIA = {
    "yes": "The request changes Markdown block structure or structured elements: converting content into or out of "
    "a table, list, heading, blockquote, code block, display math, Mermaid diagram, or meeting-notes sections, "
    "or changing links, images, checkboxes, footnotes, or frontmatter",
    "no": "Wording, tone, or content changes that keep the structure; inline bold, italic, or strikethrough alone; "
    "repairing text broken across lines inside the same kind of block",
}
LANGUAGE_QUESTION = "target_language"
LANGUAGE_NONE = "none"
LANGUAGE_OTHER = "other"
# Jev는 코드를 쓰지 않고 고르기만 해서 목록이 필요하다. 목록 밖 언어는 other로 받아 LLM이 코드를 쓰게 한다.
LANGUAGE_CRITERIA = {
    "ko": "Korean (ko)",
    "en": "English (en)",
    "ja": "Japanese (ja)",
    "zh": "Chinese (zh)",
    "es": "Spanish (es)",
    "fr": "French (fr)",
    "de": "German (de)",
    LANGUAGE_NONE: "Null: not a translation, or the target language is not stated",
    LANGUAGE_OTHER: "A translation into a stated language that is not listed here",
}
logger = logging.getLogger(__name__)


def with_jev_edit_detail(
    classifier: ChatCompletionsMarkdownEditDetailClassifier,
) -> ChatCompletionsMarkdownEditDetailClassifier | JevMarkdownEditDetailClassifier:
    """`JEV_EDIT_DETAIL_ENABLED`와 API 키가 있으면 Jev를 먼저 쓰고, 아니면 기존 LLM 분류를 그대로 쓴다."""
    client = build_jev_client(JEV_EDIT_DETAIL_ENABLED_ENV, interactive=True)
    if client is None:
        return classifier
    return JevMarkdownEditDetailClassifier(
        client, classifier, jev_edit_detail_rule(DEFAULT_MARKDOWN_EDIT_DETAIL_PROMPT.read_text(encoding="utf-8"))
    )


class JevMarkdownEditDetailClassifier:
    def __init__(
        self, client: JevClient, fallback: ChatCompletionsMarkdownEditDetailClassifier, rule: str
    ) -> None:
        self._client = client
        self._fallback = fallback
        self._rule = rule

    def classify(self, request: MarkdownEditRequest) -> MarkdownEditDetail:
        state = json.dumps({"rule": self._rule, "payload": edit_detail_payload(request)}, ensure_ascii=False)
        try:
            answers = self._client.choose(state, _questions(state))
            return _detail(answers)
        except JevUnavailable as exc:
            logger.warning("[Jev 편집 세부 분류 대체] %s", exc)
            return self._fallback.classify(request)


def jev_edit_detail_rule(prompt: str) -> str:
    """LLM 프롬프트에서 JSON 출력 형식 절을 뺀 판단 규칙. Jev는 선택지를 고를 뿐 JSON을 쓰지 않는다."""
    head, _, _ = prompt.partition("\nRequired JSON schema:")
    return head.replace("Return only one JSON object matching the required schema. ", "").rstrip()


def _questions(state: str) -> dict[str, dict[str, Any]]:
    # 라우팅과 같이 요청 상태로 정한 고정 무작위 순서로 섞어 선택지 위치가 단서가 되지 않게 한다.
    languages = list(LANGUAGE_CRITERIA)
    random.Random(hashlib.sha256(state.encode()).hexdigest()).shuffle(languages)
    return {
        FIRST_FORMAT_QUESTION: {
            "type": "choice",
            "instructions": "The main Markdown form the edited result must take.",
            "criteria": {
                FORMAT_NONE: "No specific Markdown form is required; "
                "a form named only as a source, reference, or something to preserve does not count",
                **{name: f"The request asks the result to {text}" for name, text in FORMAT_DEFINITIONS.items()},
            },
        },
        SECOND_FORMAT_QUESTION: {
            "type": "choice",
            "instructions": "A second, different Markdown form the request separately asks for, if any. Use none when there is only one.",
            "criteria": {
                FORMAT_NONE: "The request does not separately ask for a second form",
                **{name: f"The request separately asks the result to also {text}" for name, text in FORMAT_DEFINITIONS.items()},
            },
        },
        ADDITION_ONLY_QUESTION: {
            "type": "choice",
            "instructions": "Is this an addition-only request?",
            "criteria": ADDITION_ONLY_CRITERIA,
        },
        STRUCTURE_CHANGE_QUESTION: {
            "type": "choice",
            "instructions": "Does this request change Markdown structure?",
            "criteria": STRUCTURE_CHANGE_CRITERIA,
        },
        LANGUAGE_QUESTION: {
            "type": "choice",
            "instructions": "target_language",
            "criteria": {key: LANGUAGE_CRITERIA[key] for key in languages},
        },
    }


def _detail(answers: dict[str, dict[str, Any]]) -> MarkdownEditDetail:
    language = answers[LANGUAGE_QUESTION]["choice"]
    if language == LANGUAGE_OTHER:
        raise JevUnavailable("선택지에 없는 번역 언어입니다.")
    # ponytail: 형식은 요청당 2개까지 받는다. 3개 이상을 요구하는 요청이 평가에 나오면 질문을 늘린다.
    chosen = {answers[FIRST_FORMAT_QUESTION]["choice"], answers[SECOND_FORMAT_QUESTION]["choice"]}
    return MarkdownEditDetail(
        requested_formats=tuple(edit_format for edit_format in EDIT_FORMATS if edit_format in chosen),
        addition_only=answers[ADDITION_ONLY_QUESTION]["choice"] == "yes",
        structure_change=answers[STRUCTURE_CHANGE_QUESTION]["choice"] == "yes",
        target_language=None if language == LANGUAGE_NONE else language,
    )
