"""편집 세부 분류(2단계 라우팅). 라우터가 편집으로 고른 요청 문장을 다시 읽어 결과 형식·추가 전용·구조 변경·번역 언어를 정한다.

출력 검사와 원문 범위 편집은 요청 문장의 단어 대신 이 값만 본다. 라우터의 edit_goal은 넘기지 않아
대분류 판단과 독립적으로 문장을 읽게 한다.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.core.pipeline_control import PipelineRunCancelledError
from app.modules.markdown_edit.domain.entities import EDIT_FORMATS, MarkdownEditDetail, MarkdownEditRequest
from app.modules.wiki_generation.infrastructure.chat_completions_llm import ChatCompletionsJsonClient
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError

DEFAULT_MARKDOWN_EDIT_DETAIL_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "markdown_edit_detail.system.md"
logger = logging.getLogger(__name__)


class ChatCompletionsMarkdownEditDetailClassifier:
    def __init__(self, client: ChatCompletionsJsonClient, system_prompt: str | None = None) -> None:
        self._client = client
        self._system_prompt = system_prompt or DEFAULT_MARKDOWN_EDIT_DETAIL_PROMPT.read_text(encoding="utf-8")

    def classify(self, request: MarkdownEditRequest) -> MarkdownEditDetail:
        try:
            raw = self._client.complete_json(
                self._system_prompt, json.dumps(edit_detail_payload(request), ensure_ascii=False, indent=2)
            )
            return parse_edit_detail(raw)
        except PipelineRunCancelledError:
            # 취소·시한 초과는 RuntimeError 하위 클래스라 아래에서 삼키지 않도록 먼저 올린다.
            raise
        except (JsonParseError, ValueError, RuntimeError) as exc:
            # 세부 분류가 없으면(형식 오류·provider 통신 오류) 형식 검사를 건너뛰고 LLM 평가기가 요청 충족 여부를 판단한다.
            # 편집 자체는 막지 않는다.
            logger.warning("[편집 세부 분류 실패] %s", exc)
            return MarkdownEditDetail()


def edit_detail_payload(request: MarkdownEditRequest) -> dict[str, object]:
    return {
        "instruction": request.instruction,
        "conversation_summary": request.conversation_summary,
        # Skill만 실행하는 요청은 문장에 결과 형식이 없고 Skill 지침에 있다.
        "skill_instructions": request.skill_instructions or "",
    }


def parse_edit_detail(raw: object) -> MarkdownEditDetail:
    if not isinstance(raw, dict):
        raise ValueError("edit detail must be a JSON object")
    formats = raw.get("requested_formats")
    if not isinstance(formats, list) or any(item not in EDIT_FORMATS for item in formats):
        raise ValueError(f"requested_formats must be a list of {EDIT_FORMATS}")
    addition_only = raw.get("addition_only")
    structure_change = raw.get("structure_change")
    if type(addition_only) is not bool or type(structure_change) is not bool:
        raise ValueError("addition_only and structure_change must be booleans")
    language = raw.get("target_language")
    if language is not None and (not isinstance(language, str) or not re.fullmatch(r"[a-z]{2}", language)):
        raise ValueError("target_language must be an ISO 639-1 code or null")
    return MarkdownEditDetail(
        requested_formats=tuple(dict.fromkeys(formats)),
        addition_only=addition_only,
        structure_change=structure_change,
        target_language=language,
    )
