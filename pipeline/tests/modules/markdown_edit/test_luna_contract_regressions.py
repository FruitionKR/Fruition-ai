"""gpt-6-luna 통합 테스트의 markdown_output_contract_failed 회귀 테스트.

실제 실패는 두 번 모두 LLM 평가 거절이었다(편집 결과 자체는 결정적 계약을 통과).
- tag Skill: insert_after 결과 앞의 문단 구분 빈 줄을 "빈 줄도 추가했다"로 거절.
- "첫 문단을 두 문장으로… 마지막에 … 붙여": 모호한 "마지막에"를 평가 모델이 다른 해석으로 거절.
"""

import json
import unittest

from app.modules.markdown_edit.domain.entities import MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.domain.markdown_output_contract import MarkdownOutputContractError
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import (
    DEFAULT_MARKDOWN_EDIT_EVALUATOR_PROMPT,
    ChatCompletionsMarkdownEditor,
    contract_failure_code,
)

DOCUMENT = (
    "# 토양 수분 센서\n\n"
    "이 문서는 가상의 스마트팜 토양 센서 점검 기록이다. 센서는 함수율을 측정한다.\n\n"
    "## 센서 기록\n\n"
    "운영자는 배액률을 함께 살핀다.\n"
)
EVALUATOR_PROMPT = DEFAULT_MARKDOWN_EDIT_EVALUATOR_PROMPT.read_text(encoding="utf-8")


class ScriptedClient:
    """편집 응답과 평가 응답을 따로 돌려주는 fake. 평가 payload를 기록한다."""

    def __init__(self, edits: list[dict[str, object]], evaluations: list[dict[str, object]]) -> None:
        self.edits = edits
        self.evaluations = evaluations
        self.evaluation_payloads: list[dict[str, object]] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
        if system_prompt == EVALUATOR_PROMPT:
            self.evaluation_payloads.append(json.loads(user_prompt))
            return self.evaluations.pop(0)
        return self.edits.pop(0)


def luna_tag_line_output() -> dict[str, object]:
    """luna가 tag Skill에 낸 모양: 문서 끝 insert_after, 앞에 문단 구분 빈 줄."""
    return {
        "operation": "insert_after",
        "actual_target": {"type": "whole_document", "start_line": 1, "end_line": 8},
        "summary": "문서 끝에 태그 줄을 추가했습니다.",
        "replacement_markdown": "\nTAG[태그 줄을 붙여 줘] DOC[현재 문서] SEL[(선택 없음)]\n",
    }


def tag_request() -> MarkdownEditRequest:
    return MarkdownEditRequest(
        instruction="태그 줄을 붙여 줘",
        markdown=DOCUMENT,
        target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=8),
        edit_goal="other",
        edit_operation="insert_after",
        edit_destination="document_end",
    )


class LunaContractRegressionTest(unittest.TestCase):
    def test_evaluator_ignores_layout_blank_lines_around_inserted_content(self) -> None:
        client = ScriptedClient([luna_tag_line_output()], [{"passed": True, "failures": []}])
        editor = ChatCompletionsMarkdownEditor(client, "system")  # type: ignore[arg-type]

        result = editor.generate_edit(tag_request())

        self.assertEqual(
            client.evaluation_payloads[0]["replacement_lines"],
            ["TAG[태그 줄을 붙여 줘] DOC[현재 문서] SEL[(선택 없음)]"],
        )
        # 실제 삽입 결과의 문단 구분 빈 줄은 그대로 둔다.
        self.assertTrue(result.edit.replacement_markdown.startswith("\nTAG["))

    def test_replace_keeps_all_replacement_lines_for_evaluation(self) -> None:
        replacement = "# 토양 수분 센서\n\n짧은 첫 문단이다. 센서는 주 1회 보정한다.\n\n## 센서 기록\n\n운영자는 배액률을 함께 살핀다.\n"
        client = ScriptedClient(
            [{
                "operation": "replace",
                "actual_target": {"type": "whole_document", "start_line": 1, "end_line": 8},
                "summary": "첫 문단을 줄였습니다.",
                "replacement_markdown": replacement,
            }],
            [{"passed": True, "failures": []}],
        )
        editor = ChatCompletionsMarkdownEditor(client, "system")  # type: ignore[arg-type]

        editor.generate_edit(
            MarkdownEditRequest(
                instruction="첫 문단을 두 문장으로 더 간결하게 고쳐 줘. 마지막에 '센서는 주 1회 보정한다.'를 붙여.",
                markdown=DOCUMENT,
                target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=8),
                edit_goal="other",
            )
        )

        self.assertEqual(client.evaluation_payloads[0]["replacement_lines"], replacement.splitlines())

    def test_logs_contract_failure_codes_without_document_or_evaluation_text(self) -> None:
        rejection = {"passed": False, "failures": ["문서 끝에 태그 줄 외에 빈 줄도 추가했습니다. 배액률 문장을 지우세요."]}
        client = ScriptedClient(
            [luna_tag_line_output(), luna_tag_line_output()],
            [rejection, dict(rejection)],
        )
        editor = ChatCompletionsMarkdownEditor(client, "system")  # type: ignore[arg-type]

        with self.assertLogs(
            "app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor",
            level="WARNING",
        ) as logs:
            with self.assertRaises(MarkdownOutputContractError):
                editor.generate_edit(tag_request())

        output = "\n".join(logs.output)
        self.assertEqual(output.count("[Markdown 편집 계약 실패]"), 2)
        self.assertIn("attempt=1", output)
        self.assertIn("attempt=2", output)
        self.assertIn("llm_evaluation_rejected", output)
        self.assertIn("target=whole_document:1-8", output)
        self.assertNotIn("배액률", output)
        self.assertNotIn("TAG[", output)

    def test_failure_code_drops_model_values(self) -> None:
        self.assertEqual(contract_failure_code("LLM evaluation: 본문 인용"), "llm_evaluation_rejected")
        self.assertEqual(
            contract_failure_code("unknown source range segment id: 본문 조각"),
            "unknown source range segment id",
        )
        self.assertEqual(
            contract_failure_code("fenced code block opened at line 4 must be closed"),
            "fenced code block opened at line 4 must be closed",
        )

    def test_evaluator_prompt_accepts_reasonable_readings_and_layout_blank_lines(self) -> None:
        self.assertIn("never fail an insert_after proposal", EVALUATOR_PROMPT)
        self.assertIn("follows any reasonable reading", EVALUATOR_PROMPT)
