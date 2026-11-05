"""편집 세부 분류를 Jev가 고른다. 쓸 수 없으면 기존 LLM 분류로 처리한다.

Jev는 질문마다 선택지 하나만 고른다. 형식 조합은 512개라 선택지 한도(255)를 넘고, 형식마다 예/아니오로 물으면
다른 형식과 견주지 못해 "수식 블록"에 코드 블록까지 고르는 일이 반복됐다. 그래서 결과의 주 형식 하나만 고른다.
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
# 글머리표·체크리스트는 라우터 목표가 맡아 형식 값이 없다. 이 자리가 없으면 가장 비슷한 번호 목록을 고른다.
FORMAT_OTHER_LIST = "other_list"
FIRST_FORMAT_QUESTION = "first_form"
FORMAT_DEFINITIONS = {
    "meeting_notes": "rewrite the target into meeting notes with discussion, decision, and next-step sections",
    "table": "present the result as a Markdown table",
    "numbered_list": "present items as an ordered, numbered list",
    "blockquote": "put text in a blockquote",
    "heading": "add or change a Markdown heading",
    "inline_style": "apply bold, italic, or strikethrough to some text",
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
# 번역 검사가 구분하는 값은 ko·en뿐이고, 그 밖의 언어와 번역 아님은 같은 경로다(markdown_source_range._required_segment_ids).
LANGUAGE_CRITERIA = {
    "ko": "A translation into Korean (ko)",
    "en": "A translation into English (en)",
    LANGUAGE_NONE: "Not a translation, the target language is not stated, or the target is another language",
}
logger = logging.getLogger(__name__)


def with_jev_edit_detail(
    classifier: ChatCompletionsMarkdownEditDetailClassifier,
) -> ChatCompletionsMarkdownEditDetailClassifier | JevMarkdownEditDetailClassifier:
    """API 키가 있으면 Jev를 먼저 쓴다. `JEV_EDIT_DETAIL_ENABLED=false`이거나 키가 없으면 기존 LLM 분류를 그대로 쓴다."""
    client = build_jev_client(JEV_EDIT_DETAIL_ENABLED_ENV, interactive=True, default_enabled=True)
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
                FORMAT_OTHER_LIST: "The request asks for a bullet list or checklist, which are not listed as separate forms",
                FORMAT_NONE: "No specific Markdown form is required; "
                "a form named only as a source, reference, or something to preserve does not count",
                **{name: f"The request asks the result to {text}" for name, text in FORMAT_DEFINITIONS.items()},
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
    # ponytail: 형식은 요청당 1개만 고른다. 나머지 형식은 편집 모델과 평가기가 요청 문장으로 본다. 복수 요청이 흔하면 다시 본다.
    chosen = answers[FIRST_FORMAT_QUESTION]["choice"]
    return MarkdownEditDetail(
        requested_formats=(chosen,) if chosen in EDIT_FORMATS else (),
        addition_only=answers[ADDITION_ONLY_QUESTION]["choice"] == "yes",
        structure_change=answers[STRUCTURE_CHANGE_QUESTION]["choice"] == "yes",
        target_language=None if language == LANGUAGE_NONE else language,
    )
