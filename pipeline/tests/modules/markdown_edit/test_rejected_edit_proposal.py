"""평가를 통과하지 못한 답과 모델의 거절 문구가 수정안이 되지 않는지 확인한다.

거절 문구 두 개는 통합 확인에서 문서 본문을 대체했던 실제 gpt-6-luna 응답이다.
"""

import json
import unittest

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.entities import (
    ActiveMarkdownContext,
    AgentTurnRequest,
    AgentTurnRoute,
)
from app.modules.markdown_edit.application.generate_markdown_document import (
    GenerateMarkdownDocumentUseCase,
)
from app.modules.markdown_edit.application.generate_markdown_edit import (
    GenerateMarkdownEditUseCase,
)
from app.modules.markdown_edit.domain.entities import (
    MarkdownEditRequest,
    MarkdownEditTarget,
)
from app.modules.markdown_edit.domain.markdown_output_contract import (
    REFUSAL_FAILURE,
    MarkdownOutputContractError,
    validate_markdown_output,
)
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import (
    ChatCompletionsMarkdownEditor,
)
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError

HEX_TOKEN = "0123456789abcdef01234567"
DOCUMENT = (
    "# 주간 보고\n\n"
    "이번주 매출은 100만원이었다.\n\n"
    f"검증 코드는 {HEX_TOKEN} 이고 별칭은 하늬나래 이다.\n\n"
    "다음주 매출 목표는 120만원이다."
)
EDITED = DOCUMENT.replace("매출", "영업이익")
LAST_LINE = len(DOCUMENT.split("\n"))
OBSERVED_REFUSALS = (
    "보호 대상 정보가 포함되어 있어 이 문서의 내용을 재현하거나 수정할 수 없습니다.",
    "요청을 수행할 수 없습니다.",
)
PASSED = {"passed": True, "failures": []}
FAILED = {"passed": False, "failures": ["요청한 단어만 바꾸고 나머지 본문은 유지해 주세요."]}


class ScriptedClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def complete_json(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, json.loads(user_prompt)))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def proposal(text=EDITED, end_line=LAST_LINE):
    return {
        "operation": "replace",
        "summary": "수정",
        "replacement_markdown": text,
        "actual_target": {"type": "whole_document", "start_line": 1, "end_line": end_line},
    }


def request(**overrides):
    return MarkdownEditRequest(
        instruction="'매출'을 '영업이익'으로 모두 바꿔 줘. 다른 내용은 그대로 둬.",
        markdown=DOCUMENT,
        target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=LAST_LINE),
        edit_goal="other",
        **overrides,
    )


def generate(responses, edit_request=None):
    client = ScriptedClient(responses)
    editor = ChatCompletionsMarkdownEditor(client, "system")
    return client, lambda: editor.generate_edit(edit_request or request())


class NoPassingCandidateTest(unittest.TestCase):
    def test_refusal_rejected_by_evaluator_never_becomes_the_edit(self):
        for refusal in OBSERVED_REFUSALS:
            with self.subTest(refusal=refusal):
                # 거절 문구는 평가 전에 막혀 평가를 부르지 않는다.
                client, run = generate([proposal(refusal), proposal(refusal)])
                with self.assertRaises(MarkdownOutputContractError):
                    run()
                self.assertEqual(len(client.calls), 2)

    def test_edit_failing_some_criteria_is_not_returned(self):
        client, run = generate([proposal(), FAILED, proposal(), FAILED])
        with self.assertRaises(MarkdownOutputContractError) as caught:
            run()
        self.assertEqual(caught.exception.failures, ["LLM evaluation: 요청한 단어만 바꾸고 나머지 본문은 유지해 주세요."])
        self.assertEqual(len(client.calls), 4)

    def test_earlier_rejected_attempt_does_not_survive_a_broken_retry(self):
        for retry in (
            [proposal("")],
            [JsonParseError("invalid JSON")],
            [proposal(), {}],
            [proposal(), {"passed": True, "failures": ["위반"]}],
        ):
            with self.subTest(retry=retry):
                _, run = generate([proposal(), FAILED, *retry])
                with self.assertRaises(MarkdownOutputContractError):
                    run()

    def test_rejected_source_range_attempt_is_not_returned(self):
        source = {"summary": "수정", "edits": [{"id": "text-0001", "replacement": "원문 구간 시도"}]}
        client = ScriptedClient([source, FAILED, proposal("일반 시도", 1), FAILED, proposal("일반 시도", 1), FAILED])
        editor = ChatCompletionsMarkdownEditor(client, "system")
        with self.assertRaises(MarkdownOutputContractError):
            editor.generate_edit(
                MarkdownEditRequest(
                    instruction="간결하게 바꿔줘.",
                    markdown="원래 문장",
                    target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1),
                    edit_goal="cleanup",
                )
            )
        self.assertEqual(len(client.calls), 6)

    def test_passing_retry_after_rejection_is_returned_unchanged(self):
        client, run = generate([proposal("틀린 답"), FAILED, proposal(), PASSED])
        self.assertEqual(run().edit.replacement_markdown, EDITED)
        self.assertEqual(client.calls[2][1]["contract_failures"], ["LLM evaluation: 요청한 단어만 바꾸고 나머지 본문은 유지해 주세요."])


class RefusalAsDocumentBodyTest(unittest.TestCase):
    def test_refusal_is_blocked_even_when_the_evaluator_would_pass_it(self):
        for refusal in (
            *OBSERVED_REFUSALS,
            "민감한 값이 있어 진행할 수 없습니다.",
            "도와드릴 수 없습니다.",
            "I can't help with that request.",
            "I'm sorry, but I cannot modify this document.",
        ):
            with self.subTest(refusal=refusal):
                client, run = generate([proposal(refusal), proposal(refusal), PASSED, PASSED])
                with self.assertRaises(MarkdownOutputContractError):
                    run()
                # 두 번 모두 평가까지 가지 않는다.
                self.assertEqual(len(client.calls), 2)

    def test_retry_is_told_to_edit_instead_of_refusing_and_can_succeed(self):
        client, run = generate([proposal(OBSERVED_REFUSALS[0]), proposal(), PASSED])
        self.assertEqual(run().edit.replacement_markdown, EDITED)
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(any("refusal" in failure for failure in client.calls[1][1]["contract_failures"]))

    def test_refusal_in_a_source_range_edit_is_blocked_before_evaluation(self):
        source = {"summary": "수정", "edits": [{"id": "text-0001", "replacement": OBSERVED_REFUSALS[1]}]}
        client = ScriptedClient([source, proposal("다듬은 문장", 1), PASSED])
        editor = ChatCompletionsMarkdownEditor(client, "system")
        result = editor.generate_edit(
            MarkdownEditRequest(
                instruction="간결하게 바꿔줘.",
                markdown="원래 문장",
                target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1),
                edit_goal="cleanup",
            )
        )
        self.assertEqual(result.edit.replacement_markdown, "다듬은 문장")
        self.assertNotIn("proposal", client.calls[1][1])

    def test_document_with_hex_token_can_still_be_edited(self):
        client, run = generate([proposal(), PASSED])
        edit = run().edit
        self.assertIn(HEX_TOKEN, edit.replacement_markdown)
        self.assertIn("영업이익", edit.replacement_markdown)
        self.assertEqual(len(client.calls), 2)

    def test_sentences_that_only_look_like_refusals_are_allowed(self):
        cases = {
            "요청에 적힌 문구": ("문장을 '요청을 수행할 수 없습니다.'로 바꿔 줘.", "원래 문장", "요청을 수행할 수 없습니다."),
            "원문에 있던 문구": ("맞춤법만 고쳐 줘.", "요청을 수행할 수 없습니다..", "요청을 수행할 수 없습니다."),
            "원문을 다듬은 규정": ("문장으로 풀어 줘.", "승인 전 문서 수정 불가.", "승인 전에는 문서를 수정할 수 없습니다."),
            "원문을 다듬은 안내": ("정중하게 바꿔 줘.", "환불은 안 됩니다.", "환불을 진행할 수 없습니다."),
            "원문의 부정 표현을 풀어 쓴 문장": ("공손한 문장으로 바꿔 줘.", "처리 불가능", "처리할 수 없습니다."),
            "원문의 부정 표현을 풀어 쓴 긴 문장": ("공손한 문장으로 바꿔 줘.", "오늘 안에 변경 불가능합니다", "오늘 안에 변경할 수 없습니다."),
            "영어 원문을 다듬은 사과": (
                "Fix the grammar.", "Sorry I cannot attend the review meeting.",
                "I'm sorry, but I cannot attend the review meeting.",
            ),
        }
        for name, (instruction, source, replacement) in cases.items():
            with self.subTest(name):
                self.assertEqual(
                    validate_markdown_output(
                        MarkdownEditRequest(
                            instruction=instruction,
                            markdown=source,
                            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1),
                            edit_goal="other",
                        ),
                        replacement,
                    ),
                    [],
                )

    def test_refusal_unrelated_to_a_source_with_negation_is_still_blocked(self):
        sources = (
            "처리 불가능",
            "재고 없음",
            "수정하지 못함",
            "재고가 없는 품목은 제외한다.",
            "처리 불가능한 건은 담당자에게 넘긴다.",
            "권한이 없는 사용자는 문서를 볼 수 없다.",
            f"{DOCUMENT}\n\n재고가 없는 품목은 처리 불가능하다.",
        )
        cases = [(source, refusal) for source in sources for refusal in OBSERVED_REFUSALS]
        # "잘못"은 부정 표현이 아니라 비교에서 빠지지 않는다.
        cases.append(("잘못된 값은 처리 불가능", "요청을 처리할 수 없습니다."))
        for source, refusal in cases:
            with self.subTest(source=source, refusal=refusal):
                self.assertIn(
                    REFUSAL_FAILURE,
                    validate_markdown_output(
                        MarkdownEditRequest(
                            instruction="공손한 문장으로 바꿔 줘.",
                            markdown=source,
                            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=len(source.split("\n"))),
                            edit_goal="other",
                        ),
                        refusal,
                    ),
                )

    def test_translation_is_not_judged_as_a_refusal(self):
        self.assertEqual(
            validate_markdown_output(
                MarkdownEditRequest(
                    instruction="한국어로 번역해 줘.",
                    markdown="This document cannot be edited before approval.",
                    target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1),
                    edit_goal="translate",
                ),
                "이 문서는 승인 전에 수정할 수 없습니다.",
            ),
            [],
        )

    def test_insert_after_is_not_judged_as_a_refusal(self):
        self.assertEqual(
            validate_markdown_output(
                MarkdownEditRequest(
                    instruction="승인 규칙을 한 줄 덧붙여 줘.",
                    markdown=DOCUMENT,
                    target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=LAST_LINE),
                    edit_goal="other",
                    edit_operation="insert_after",
                    edit_destination="document_end",
                ),
                "승인 전에는 수정할 수 없습니다.",
            ),
            [],
        )


class RecordingRunStarter:
    def __init__(self):
        self.requests = []

    def start(self, start_request):
        self.requests.append(start_request)
        return "run-1", "awaiting_approval"


class AgentTurnWithoutPassingEditTest(unittest.TestCase):
    def execute(self, client, action, persist, starter=None):
        editor = ChatCompletionsMarkdownEditor(client, "system")
        route = AgentTurnRoute(
            action=action,
            confidence=1.0,
            reason="test",
            edit_goal="other",
            edit_operation="replace",
            edit_destination="target",
            document_operation="edit",
            persist=persist,
        )

        class Router:
            def route(self, _request):
                return route

        use_case = HandleAgentTurnUseCase(
            router=Router(),  # type: ignore[arg-type]
            query_use_case=object(),  # type: ignore[arg-type]
            markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
            markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
            agent_run_starter=starter,  # type: ignore[arg-type]
        )
        return use_case.execute(
            AgentTurnRequest(
                message="'매출'을 '영업이익'으로 모두 바꿔 줘.",
                workspace_id="workspace-1",
                user_id="user-1",
                document_id="document-1",
                base_version=3,
                skill_mode="off",
                active_markdown_context=ActiveMarkdownContext(
                    markdown=DOCUMENT,
                    target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=LAST_LINE),
                ),
            )
        )

    def test_no_run_starts_so_nothing_can_be_auto_approved_or_accepted(self):
        for name, responses in {
            "평가 불합격": [proposal(), FAILED, proposal(), FAILED],
            "거절 문구": [proposal(OBSERVED_REFUSALS[0]), proposal(OBSERVED_REFUSALS[1])],
        }.items():
            with self.subTest(name):
                starter = RecordingRunStarter()
                with self.assertRaises(MarkdownOutputContractError):
                    self.execute(ScriptedClient(responses), "workspace_workflow", True, starter)
                self.assertEqual(starter.requests, [])

    def test_preview_turn_returns_no_edit_to_accept(self):
        with self.assertRaises(MarkdownOutputContractError):
            self.execute(ScriptedClient([proposal(), FAILED, proposal(), FAILED]), "markdown_edit", False)

    def test_passing_edit_still_starts_the_run(self):
        starter = RecordingRunStarter()
        result = self.execute(ScriptedClient([proposal(), PASSED]), "workspace_workflow", True, starter)
        self.assertEqual(result.run_id, "run-1")
        self.assertEqual(starter.requests[0].content.markdown, EDITED)


if __name__ == "__main__":
    unittest.main()
