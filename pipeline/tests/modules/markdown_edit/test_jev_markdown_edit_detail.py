import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.core.jev_client import JEV_API_KEY_ENV, JEV_EDIT_DETAIL_ENABLED_ENV, JevClient, JevUnavailable
from app.core.llm_prompt import LLM_SECURITY_BOUNDARY
from app.core.pipeline_control import DeadlineExceededError, PipelineRunCancelledError
from app.modules.markdown_edit.domain.entities import EDIT_FORMATS, MarkdownEditDetail, MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_edit_detail_classifier import (
    ChatCompletionsMarkdownEditDetailClassifier,
)
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import build_markdown_editor
from app.modules.markdown_edit.infrastructure.jev_markdown_edit_detail_classifier import (
    ADDITION_ONLY_QUESTION,
    FIRST_FORMAT_QUESTION,
    FORMAT_DEFINITIONS,
    FORMAT_NONE,
    FORMAT_OTHER_LIST,
    LANGUAGE_NONE,
    LANGUAGE_QUESTION,
    STRUCTURE_CHANGE_QUESTION,
    JevMarkdownEditDetailClassifier,
    jev_edit_detail_rule,
    with_jev_edit_detail,
)

REQUEST = MarkdownEditRequest(
    instruction="회의록을 바탕으로 업무를 표로 나누고 담당자를 굵게 표시해줘",
    markdown="본문",
    target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1),
    edit_goal="convert_format",
    conversation_summary="이전 대화",
    skill_instructions="Skill 지침",
)
FALLBACK_DETAIL = MarkdownEditDetail(requested_formats=("heading",))


class FakeFallback:
    def __init__(self) -> None:
        self.calls = 0

    def classify(self, request: MarkdownEditRequest) -> MarkdownEditDetail:
        self.calls += 1
        return FALLBACK_DETAIL


def answers(
    first: str = FORMAT_NONE,
    *,
    addition_only: bool = False,
    structure_change: bool = False,
    language: str = LANGUAGE_NONE,
) -> dict[str, dict[str, str]]:
    return {
        FIRST_FORMAT_QUESTION: {"choice": first},
        ADDITION_ONLY_QUESTION: {"choice": "yes" if addition_only else "no"},
        STRUCTURE_CHANGE_QUESTION: {"choice": "yes" if structure_change else "no"},
        LANGUAGE_QUESTION: {"choice": language},
    }


class FakeJev:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    def choose(self, state: str, questions: dict) -> object:
        self.calls.append((state, questions))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def classifier(jev: object, fallback: object | None = None) -> JevMarkdownEditDetailClassifier:
    return JevMarkdownEditDetailClassifier(jev, fallback or FakeFallback(), "rule")  # type: ignore[arg-type]


class JevMarkdownEditDetailClassifierTest(unittest.TestCase):
    def test_one_format_and_every_field_come_from_one_request(self) -> None:
        jev = FakeJev(answers("table", structure_change=True, language="en"))

        detail = classifier(jev).classify(REQUEST)

        self.assertEqual(detail, MarkdownEditDetail(
            requested_formats=("table",), addition_only=False, structure_change=True, target_language="en",
        ))
        self.assertEqual(len(jev.calls), 1)
        self.assertEqual(
            set(jev.calls[0][1]),
            {FIRST_FORMAT_QUESTION, ADDITION_ONLY_QUESTION, STRUCTURE_CHANGE_QUESTION, LANGUAGE_QUESTION},
        )

    def test_every_format_can_be_chosen_and_criteria_carry_its_definition(self) -> None:
        # 선택지에 뜻이 없으면 Jev가 긴 규칙만 보고 고르다 틀렸다. 정의를 선택지마다 둔다.
        self.assertEqual(set(FORMAT_DEFINITIONS), set(EDIT_FORMATS))
        jev = FakeJev(answers())

        classifier(jev).classify(REQUEST)

        criteria = jev.calls[0][1][FIRST_FORMAT_QUESTION]["criteria"]
        self.assertEqual(set(criteria), {FORMAT_NONE, FORMAT_OTHER_LIST, *EDIT_FORMATS})
        for edit_format in EDIT_FORMATS:
            self.assertIn(FORMAT_DEFINITIONS[edit_format], criteria[edit_format])

    def test_no_selection_means_no_detail(self) -> None:
        self.assertEqual(classifier(FakeJev(answers())).classify(REQUEST), MarkdownEditDetail())

    def test_state_has_rule_once_and_same_input_as_llm_without_edit_goal(self) -> None:
        jev = FakeJev(answers())

        classifier(jev).classify(REQUEST)

        state, questions = jev.calls[0]
        self.assertEqual(json.loads(state), {
            "rule": "rule",
            "payload": {
                "instruction": REQUEST.instruction,
                "conversation_summary": "이전 대화",
                "skill_instructions": "Skill 지침",
            },
        })
        self.assertFalse(any("rule" in question["instructions"] for question in questions.values()))

    def test_rule_drops_json_output_instructions_only(self) -> None:
        rule = jev_edit_detail_rule(
            "Intro. Return only one JSON object matching the required schema. Keep this.\n\n"
            "target_language: rule.\n\nRequired JSON schema:\n{}\n"
        )

        self.assertEqual(rule, "Intro. Keep this.\n\ntarget_language: rule.")

    def test_bullet_or_checklist_choice_means_no_requested_format(self) -> None:
        # 라우터 목표가 맡는 목록이라 형식으로 넘기지 않는다. 번호 목록으로 고르면 체크리스트 검사와 충돌한다.
        detail = classifier(FakeJev(answers(FORMAT_OTHER_LIST))).classify(REQUEST)

        self.assertEqual(detail, MarkdownEditDetail())

    def test_jev_failure_goes_to_llm(self) -> None:
        fallback = FakeFallback()

        with self.assertLogs(level="WARNING"):
            detail = classifier(FakeJev(JevUnavailable("HTTP 402")), fallback).classify(REQUEST)

        self.assertEqual(detail, FALLBACK_DETAIL)
        self.assertEqual(fallback.calls, 1)

    def test_cancellation_is_not_swallowed(self) -> None:
        for error in (PipelineRunCancelledError("cancelled"), DeadlineExceededError("deadline")):
            fallback = FakeFallback()
            with self.subTest(error=error), self.assertRaises(type(error)):
                classifier(FakeJev(error), fallback).classify(REQUEST)
            self.assertEqual(fallback.calls, 0)

    def test_real_client_accepts_the_request_and_adds_security_boundary_once(self) -> None:
        bodies: list[dict] = []

        def handler(http_request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(http_request.content))
            return httpx.Response(200, json={"answers": answers("table"), "usage": {}})

        client = JevClient("key", http=httpx.Client(transport=httpx.MockTransport(handler)))

        detail = classifier(client).classify(REQUEST)

        self.assertEqual(detail, MarkdownEditDetail(requested_formats=("table",)))
        state = json.loads(bodies[0]["state"])
        self.assertEqual(state["security_boundary"], LLM_SECURITY_BOUNDARY)
        self.assertEqual(state["payload"]["instruction"], REQUEST.instruction)


class WithJevEditDetailTest(unittest.TestCase):
    def test_off_or_without_key_keeps_llm_classifier(self) -> None:
        llm = ChatCompletionsMarkdownEditDetailClassifier(object(), "system")  # type: ignore[arg-type]
        for env in ({}, {JEV_EDIT_DETAIL_ENABLED_ENV: "true"}, {JEV_API_KEY_ENV: "key"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertIs(with_jev_edit_detail(llm), llm)

    def test_editor_uses_jev_when_enabled(self) -> None:
        with patch.dict(os.environ, {
            JEV_EDIT_DETAIL_ENABLED_ENV: "true", JEV_API_KEY_ENV: "key", "OPENAI_API_KEY": "key",
        }):
            editor = build_markdown_editor(provider="openai", model="gpt-6-luna")

        self.assertIsInstance(editor._detail_classifier, JevMarkdownEditDetailClassifier)  # type: ignore[attr-defined]
        rule = editor._detail_classifier._rule  # type: ignore[attr-defined]
        self.assertIn("requested_formats:", rule)
        self.assertNotIn("JSON", rule)

    def test_editor_keeps_llm_when_disabled(self) -> None:
        with patch.dict(os.environ, {JEV_EDIT_DETAIL_ENABLED_ENV: "false", "OPENAI_API_KEY": "key"}):
            editor = build_markdown_editor(provider="openai", model="gpt-6-luna")

        self.assertIsInstance(
            editor._detail_classifier,  # type: ignore[attr-defined]
            ChatCompletionsMarkdownEditDetailClassifier,
        )


if __name__ == "__main__":
    unittest.main()
