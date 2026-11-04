import json
import unittest
from pathlib import Path

from app.core.pipeline_control import DeadlineExceededError, PipelineRunCancelledError
from app.modules.markdown_edit.domain.entities import MarkdownEditDetail, MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.domain.markdown_output_contract import MarkdownOutputContractError
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_edit_detail_classifier import (
    DEFAULT_MARKDOWN_EDIT_DETAIL_PROMPT,
    ChatCompletionsMarkdownEditDetailClassifier,
)
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import ChatCompletionsMarkdownEditor
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError

TARGET = MarkdownEditTarget(type="whole_document", start_line=1, end_line=1)
WHOLE = {"type": "whole_document", "start_line": 1, "end_line": 1}


class FixedJsonClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> object:
        self.calls.append((system_prompt, user_prompt))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def request(**overrides: object) -> MarkdownEditRequest:
    values: dict[str, object] = {
        "instruction": "회의록을 반영해서 각 항목 아래에 한 줄씩 채워줘",
        "markdown": "1. 배경",
        "target": TARGET,
        "edit_goal": "convert_format",
        "conversation_summary": "이전 대화",
    }
    values.update(overrides)
    return MarkdownEditRequest(**values)  # type: ignore[arg-type]


class MarkdownEditDetailClassifierTest(unittest.TestCase):
    def test_parses_classified_detail(self) -> None:
        client = FixedJsonClient({
            "requested_formats": ["table", "table", "bold"],
            "addition_only": True,
            "structure_change": False,
            "target_language": None,
        })

        detail = ChatCompletionsMarkdownEditDetailClassifier(client, "system").classify(request())

        self.assertEqual(
            detail,
            MarkdownEditDetail(requested_formats=("table", "bold"), addition_only=True),
        )

    def test_reads_request_sentence_without_router_edit_goal(self) -> None:
        # 대분류 판단에 끌려가지 않도록 세부 분류기는 라우터의 edit_goal을 보지 않는다.
        client = FixedJsonClient({
            "requested_formats": [], "addition_only": False, "structure_change": False, "target_language": None,
        })

        ChatCompletionsMarkdownEditDetailClassifier(client, "system").classify(request())

        payload = json.loads(client.calls[0][1])
        self.assertEqual(
            payload,
            {"instruction": request().instruction, "conversation_summary": "이전 대화", "skill_instructions": ""},
        )

    def test_skill_run_sends_skill_instructions(self) -> None:
        # Skill만 실행하는 요청은 결과 형식이 문장이 아니라 Skill 지침에 있다.
        client = FixedJsonClient({
            "requested_formats": ["table"], "addition_only": False, "structure_change": True, "target_language": None,
        })

        ChatCompletionsMarkdownEditDetailClassifier(client, "system").classify(
            request(instruction="Skill 지침을 적용해 주세요", skill_instructions="결과를 표로 정리한다."),
        )

        self.assertEqual(json.loads(client.calls[0][1])["skill_instructions"], "결과를 표로 정리한다.")

    def test_invalid_or_failed_classification_means_no_detail(self) -> None:
        invalid_outputs: tuple[object, ...] = (
            {"requested_formats": ["checklist"], "addition_only": False, "structure_change": False},
            {"requested_formats": [], "addition_only": "yes", "structure_change": False},
            {"requested_formats": [], "addition_only": False, "structure_change": False, "target_language": "English"},
            ["table"],
            JsonParseError("broken"),
            RuntimeError("provider HTTP 503"),
        )
        for output in invalid_outputs:
            with self.subTest(output=output):
                with self.assertLogs(level="WARNING"):
                    detail = ChatCompletionsMarkdownEditDetailClassifier(
                        FixedJsonClient(output), "system",
                    ).classify(request())

                self.assertEqual(detail, MarkdownEditDetail())

    def test_cancellation_is_not_swallowed(self) -> None:
        # 취소·시한 초과는 RuntimeError 하위 클래스지만 편집을 이어가지 않도록 그대로 올린다.
        for error in (PipelineRunCancelledError("cancelled"), DeadlineExceededError("deadline")):
            with self.subTest(error=error), self.assertRaises(type(error)):
                ChatCompletionsMarkdownEditDetailClassifier(FixedJsonClient(error), "system").classify(request())


class RecordingClassifier:
    def __init__(self, detail: MarkdownEditDetail) -> None:
        self.detail = detail
        self.requests: list[MarkdownEditRequest] = []

    def classify(self, request: MarkdownEditRequest) -> MarkdownEditDetail:
        self.requests.append(request)
        return self.detail


class EditorUsesClassifiedDetailTest(unittest.TestCase):
    def test_editor_sends_classified_formats_and_checks_them(self) -> None:
        edit_outputs = [
            {"operation": "replace", "actual_target": WHOLE, "summary": "정리했습니다.", "replacement_markdown": "배경 설명"},
            {"operation": "replace", "actual_target": WHOLE, "summary": "회의록으로 바꿨습니다.",
             "replacement_markdown": "## 논의 사항\n배경\n\n## 결정 사항\n없음\n\n## 다음 작업\n없음"},
        ]
        calls: list[dict[str, object]] = []

        class Client:
            def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
                if system_prompt == "evaluator":
                    return {"passed": True, "failures": []}
                calls.append(json.loads(user_prompt))
                return edit_outputs.pop(0)

        classifier = RecordingClassifier(MarkdownEditDetail(requested_formats=("meeting_notes",)))
        editor = ChatCompletionsMarkdownEditor(Client(), "system", detail_classifier=classifier)  # type: ignore[arg-type]
        editor._evaluation_system_prompt = "evaluator"

        result = editor.generate_edit(request(instruction="이 메모를 회의록으로 정리해줘", markdown="배경"))

        self.assertEqual(len(classifier.requests), 1)
        self.assertEqual(calls[0]["requested_formats"], ["meeting_notes"])
        self.assertIn("meeting notes must contain `## 논의 사항`", calls[1]["contract_failures"])
        self.assertIn("## 논의 사항", result.edit.replacement_markdown)


if __name__ == "__main__":
    unittest.main()


class ReplacementRequestClassifiedAsAdditiveTest(unittest.TestCase):
    """"'없음.' 대신 적어줘"를 addition_only로 분류하면 어떤 답도 통과하지 못한다(실서버 Flash-Lite 편집 실패 재현)."""

    SOURCE = "## 담당자\n\n없음."
    REPLACED = "## 담당자\n\n민수"
    KEPT = "## 담당자\n\n없음.\n\n민수"
    INSTRUCTION = "'없음.' 대신 민수라고 적어줘"

    def _edit(self, detail: MarkdownEditDetail, *replacements: str) -> tuple[object, list[str]]:
        outputs = [
            {"operation": "replace", "actual_target": {"type": "whole_document", "start_line": 1, "end_line": 3},
             "summary": "담당자를 적었습니다.", "replacement_markdown": replacement}
            for replacement in replacements
        ]
        evaluated: list[str] = []

        class Client:
            def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
                if system_prompt == "evaluator":
                    replacement = json.loads(user_prompt)["proposal"]["replacement_markdown"]
                    evaluated.append(replacement)
                    # 평가기는 요청대로 '없음.'이 빠졌는지 본다.
                    passed = "없음." not in replacement
                    return {"passed": passed, "failures": [] if passed else ["'없음.'을 지우고 적어야 합니다."]}
                return outputs.pop(0)

        editor = ChatCompletionsMarkdownEditor(  # type: ignore[arg-type]
            Client(), "system", detail_classifier=RecordingClassifier(detail)
        )
        editor._evaluation_system_prompt = "evaluator"
        target = MarkdownEditTarget(type="whole_document", start_line=1, end_line=3)
        request_value = MarkdownEditRequest(
            instruction=self.INSTRUCTION, markdown=self.SOURCE, target=target, edit_goal="other",
        )
        return editor.generate_edit(request_value), evaluated

    def test_additive_classification_fails_both_ways_the_model_can_answer(self) -> None:
        with self.assertRaises(MarkdownOutputContractError) as caught:
            self._edit(MarkdownEditDetail(addition_only=True), self.REPLACED, self.KEPT)

        self.assertTrue(any(failure.startswith("LLM evaluation:") for failure in caught.exception.failures))

    def test_additive_check_still_rejects_replacing_when_classified_additive(self) -> None:
        with self.assertRaises(MarkdownOutputContractError) as caught:
            self._edit(MarkdownEditDetail(addition_only=True), self.REPLACED, self.REPLACED)

        self.assertIn("additive edit must preserve every existing non-empty line in order", caught.exception.failures)

    def test_replacement_classification_succeeds_on_first_answer(self) -> None:
        result, evaluated = self._edit(MarkdownEditDetail(addition_only=False), self.REPLACED)

        self.assertEqual(result.edit.replacement_markdown, self.REPLACED)  # type: ignore[attr-defined]
        self.assertEqual(evaluated, [self.REPLACED])

    def test_prompt_and_eval_define_replacement_wording_as_not_additive(self) -> None:
        prompt = DEFAULT_MARKDOWN_EDIT_DETAIL_PROMPT.read_text(encoding="utf-8")
        self.assertIn("'없음.' 대신", prompt)
        dataset = Path(__file__).resolve().parents[3] / "evals" / "markdown_edit_detail.jsonl"
        cases = {
            case["id"]: case
            for case in map(json.loads, dataset.read_text(encoding="utf-8").splitlines())
        }
        self.assertFalse(cases["replace-instead-of"]["expected"]["addition_only"])
        self.assertTrue(cases["mention-instead-but-adds"]["expected"]["addition_only"])
