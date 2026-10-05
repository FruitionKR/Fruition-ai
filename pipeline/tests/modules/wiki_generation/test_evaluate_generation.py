import unittest

from app.modules.wiki_generation.application.evaluate_generation import evaluate_generation
from app.modules.wiki_generation.domain.entities import SourceBlock, SourceDocument


class MalformedEvaluationCompletion:
    def complete_json(self, _system_prompt: str, _user_prompt: str) -> dict:
        return {
            "scores": {"overall": "high", "concept_groundedness": 0.5},
            "passed": "true",
            "retry_recommended": "false",
            "issues": ["invalid issue"],
            "warnings": {"type": "optional_improvement"},
            "retry_feedback": [],
        }


class MinimalEvaluationCompletion:
    def complete_json(self, _system_prompt: str, _user_prompt: str) -> dict:
        return {"passed": True}


class EvaluateGenerationTest(unittest.TestCase):
    def test_malformed_evaluator_response_becomes_retryable_failure(self) -> None:
        evaluation = evaluate_generation(
            completion=MalformedEvaluationCompletion(),
            evaluator_prompt="evaluate",
            document=SourceDocument(
                document_id="doc-1",
                title="문서",
                source_path="document.md",
                content_sha1="sha1",
            ),
            blocks=[
                SourceBlock(
                    document_id="doc-1",
                    block_id="B0001",
                    source_reference_id="ref-1",
                    text="근거",
                    line_start=1,
                    line_end=1,
                )
            ],
            normalized={"concept_ledger": [], "observations": []},
        )

        self.assertEqual(evaluation["scores"], {"concept_groundedness": 0.5})
        self.assertFalse(evaluation["passed"])
        self.assertTrue(evaluation["retry_recommended"])
        self.assertEqual(evaluation["warnings"], [])
        self.assertEqual(evaluation["issues"][0]["type"], "invalid_evaluator_response")
        self.assertIn("evaluator 응답 형식", evaluation["retry_feedback"])

    def test_missing_optional_evaluator_fields_keep_existing_defaults(self) -> None:
        evaluation = evaluate_generation(
            completion=MinimalEvaluationCompletion(),
            evaluator_prompt="evaluate",
            document=SourceDocument("doc-1", "문서", "document.md", "sha1"),
            blocks=[],
            normalized={"concept_ledger": [], "observations": []},
        )

        self.assertTrue(evaluation["passed"])
        self.assertFalse(evaluation["retry_recommended"])
        self.assertEqual(evaluation["scores"], {})
        self.assertEqual(evaluation["issues"], [])
        self.assertEqual(evaluation["warnings"], [])
        self.assertEqual(evaluation["retry_feedback"], "")

    def test_large_document_is_evaluated_by_block_windows_and_merged(self) -> None:
        completion = WindowRecordingCompletion()
        blocks = [
            SourceBlock("doc-1", f"B{index:04d}", f"B{index:04d}", "가" * 50_000, index, index)
            for index in range(1, 5)
        ]
        normalized = {
            "evidence_units": [
                {"evidence_id": "ev-1", "claim": "첫 구간 근거", "anchor_reference_ids": ["B0001"]},
                {"evidence_id": "ev-4", "claim": "마지막 구간 근거", "anchor_reference_ids": ["B0004"]},
            ],
            "concept_ledger": [{"slug": "unanchored", "anchor_reference_ids": []}],
        }

        evaluation = evaluate_generation(
            completion=completion,
            evaluator_prompt="evaluate",
            document=SourceDocument("doc-1", "문서", "document.md", "sha1"),
            blocks=blocks,
            normalized=normalized,
        )

        self.assertEqual(len(completion.payloads), 2)
        first, second = sorted(completion.payloads, key=lambda payload: payload["source_blocks"][0]["block_id"])
        self.assertEqual([block["block_id"] for block in first["source_blocks"]], ["B0001", "B0002"])
        self.assertEqual([item["evidence_id"] for item in first["normalized"]["evidence_units"]], ["ev-1"])
        self.assertEqual([item["evidence_id"] for item in second["normalized"]["evidence_units"]], ["ev-4"])
        self.assertEqual(first["normalized"]["concept_ledger"], normalized["concept_ledger"])
        self.assertEqual(second["normalized"]["concept_ledger"], [])
        self.assertFalse(evaluation["passed"])
        self.assertEqual(evaluation["scores"], {"overall": 0.5})
        self.assertEqual(
            [issue["target"] for issue in evaluation["issues"] if issue.get("type") == "window_issue"],
            [["B0003"]],
        )

    def test_small_document_keeps_single_request(self) -> None:
        completion = WindowRecordingCompletion()
        evaluate_generation(
            completion=completion,
            evaluator_prompt="evaluate",
            document=SourceDocument("doc-1", "문서", "document.md", "sha1"),
            blocks=[SourceBlock("doc-1", "B0001", "B0001", "근거", 1, 1)],
            normalized={},
        )

        self.assertEqual(len(completion.payloads), 1)


class WindowRecordingCompletion:
    """구간마다 받은 입력을 기록하고, B0003이 든 구간만 실패로 판정한다."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def complete_json(self, _system_prompt: str, user_prompt: str) -> dict:
        import json

        payload = json.loads(user_prompt)
        self.payloads.append(payload)
        block_ids = [block["block_id"] for block in payload["source_blocks"]]
        if "B0003" in block_ids:
            return {
                "scores": {"overall": 0.0},
                "passed": False,
                "issues": [{"type": "window_issue", "target": ["B0003"]}],
                "retry_feedback": "B0003 다시 추출",
            }
        return {"scores": {"overall": 1.0}, "passed": True}


if __name__ == "__main__":
    unittest.main()
