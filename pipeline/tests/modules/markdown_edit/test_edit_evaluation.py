import json
import unittest
from unittest.mock import Mock

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
    MarkdownOutputContractError,
)
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import (
    ChatCompletionsMarkdownEditor,
)
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError


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


def proposal(text="수정한 문장"):
    return {
        "operation": "replace",
        "summary": "수정",
        "replacement_markdown": text,
        "actual_target": {"type": "whole_document", "start_line": 1, "end_line": 1},
    }


PASSED = {"passed": True, "failures": []}
FAILED = {"passed": False, "failures": ["요청한 제약을 지켜 주세요."]}


class EditEvaluationTest(unittest.TestCase):
    def test_selection_evaluator_receives_only_bounded_surrounding_context(self):
        for goal in ("cleanup", "convert_format"):
            with self.subTest(goal=goal):
                raw = (
                    {
                        "summary": "수정",
                        "edits": [{"id": "text-0001", "replacement": "수정한 문장"}],
                    }
                    if goal == "cleanup"
                    else proposal()
                )
                raw["actual_target"] = {
                    "type": "selection",
                    "start_line": 3,
                    "end_line": 3,
                }
                client = ScriptedClient([raw, PASSED])
                request = MarkdownEditRequest(
                    instruction="앞뒤 문맥에 맞춰 다듬어줘.",
                    markdown="범위 밖 앞\n앞 문맥\n원래 문장\n뒤 문맥\n범위 밖 뒤",
                    target=MarkdownEditTarget(
                        type="selection", start_line=3, end_line=3
                    ),
                    edit_goal=goal,
                )
                ChatCompletionsMarkdownEditor(
                    client, "system", context_lines=1
                ).generate_edit(request)
                payload = client.calls[1][1]
                self.assertEqual(payload["original_markdown"], "원래 문장")
                self.assertEqual(
                    payload["surrounding_context"],
                    {"before": "앞 문맥", "after": "뒤 문맥"},
                )
                self.assertNotIn("범위 밖", json.dumps(payload, ensure_ascii=False))

    def test_agent_turn_cannot_return_an_edit_rejected_by_evaluator(self):
        client = ScriptedClient([proposal(), FAILED, proposal(), FAILED])
        editor = ChatCompletionsMarkdownEditor(client, "system")
        router = Mock()
        router.route.return_value = AgentTurnRoute(
            action="markdown_edit",
            confidence=1.0,
            reason="test",
            edit_goal="convert_format",
            document_operation="edit",
        )
        use_case = HandleAgentTurnUseCase(
            router=router,
            query_use_case=Mock(),
            markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
            markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
        )
        with self.assertRaises(MarkdownOutputContractError):
            use_case.execute(
                AgentTurnRequest(
                    message="간결하게 바꿔줘.",
                    skill_mode="off",
                    active_markdown_context=ActiveMarkdownContext(markdown="원래 문장"),
                )
            )
        self.assertEqual(len(client.calls), 4)

    def request(self, goal="convert_format"):
        return MarkdownEditRequest(
            instruction="간결하게 바꿔줘.",
            markdown="원래 문장",
            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1),
            edit_goal=goal,
            skill_instructions="의미 유지",
            output_language="ko",
        )

    def test_pass_returns_immediately_and_evaluates_final_content(self):
        client = ScriptedClient([proposal(), PASSED])
        result = ChatCompletionsMarkdownEditor(client, "system").generate_edit(
            self.request()
        )
        self.assertEqual(len(client.calls), 2)
        evaluation = client.calls[1][1]
        self.assertEqual(evaluation["instruction"], "간결하게 바꿔줘.")
        self.assertEqual(evaluation["original_markdown"], "원래 문장")
        self.assertEqual(
            evaluation["proposal"]["replacement_markdown"],
            result.edit.replacement_markdown,
        )
        self.assertEqual(
            evaluation["replacement_lines"], [result.edit.replacement_markdown]
        )
        self.assertEqual(evaluation["skill_instructions"], "의미 유지")
        self.assertEqual(evaluation["output_language"], "ko")
        self.assertNotIn("generation_constraints", evaluation)

    def test_failed_evaluation_retries_with_feedback_once(self):
        client = ScriptedClient([proposal(), FAILED, proposal("간결한 문장"), PASSED])
        result = ChatCompletionsMarkdownEditor(client, "system").generate_edit(
            self.request()
        )
        self.assertEqual(result.edit.replacement_markdown, "간결한 문장")
        self.assertEqual(len(client.calls), 4)
        self.assertIn(
            "LLM evaluation: 요청한 제약을 지켜 주세요.",
            client.calls[2][1]["contract_failures"],
        )

    def test_rejected_or_invalid_evaluation_never_returns_proposal(self):
        for verdict in [
            FAILED,
            {},
            {"passed": "true", "failures": []},
            {"passed": True, "failures": ["위반"]},
            {"passed": False, "failures": []},
            {"passed": False, "failures": [None]},
            JsonParseError("invalid JSON"),
        ]:
            with self.subTest(verdict=verdict):
                client = ScriptedClient([proposal(), verdict, proposal(), verdict])
                with self.assertRaises(MarkdownOutputContractError):
                    ChatCompletionsMarkdownEditor(client, "system").generate_edit(
                        self.request()
                    )
                self.assertEqual(len(client.calls), 4)

    def test_source_range_edit_uses_same_evaluation_and_retry_limit(self):
        source = {
            "summary": "수정",
            "edits": [{"id": "text-0001", "replacement": "정리한 문장"}],
        }
        for verdict in [PASSED, FAILED]:
            with self.subTest(verdict=verdict):
                client = ScriptedClient([source, FAILED, source, verdict])
                editor = ChatCompletionsMarkdownEditor(client, "system")
                if verdict["passed"]:
                    self.assertEqual(
                        editor.generate_edit(
                            self.request("cleanup")
                        ).edit.replacement_markdown,
                        "정리한 문장",
                    )
                else:
                    with self.assertRaises(MarkdownOutputContractError):
                        editor.generate_edit(self.request("cleanup"))
                self.assertEqual(len(client.calls), 4)
                self.assertEqual(client.calls[1][1]["original_markdown"], "원래 문장")
                self.assertIn(
                    "LLM evaluation:", client.calls[2][1]["contract_failures"][0]
                )
                self.assertEqual(
                    client.calls[2][1]["previous_assembled_markdown"], "정리한 문장"
                )

    def test_invalid_generation_skips_evaluator_and_shares_retry_budget(self):
        client = ScriptedClient([proposal(""), proposal(), FAILED])
        with self.assertRaises(MarkdownOutputContractError):
            ChatCompletionsMarkdownEditor(client, "system").generate_edit(
                self.request()
            )
        self.assertEqual(len(client.calls), 3)
        self.assertNotIn("proposal", client.calls[1][1])
        self.assertIn("proposal", client.calls[2][1])

    def test_evaluator_transport_failure_does_not_accept_proposal(self):
        client = ScriptedClient([proposal(), RuntimeError("provider unavailable")])
        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            ChatCompletionsMarkdownEditor(client, "system").generate_edit(
                self.request()
            )
        self.assertEqual(len(client.calls), 2)
