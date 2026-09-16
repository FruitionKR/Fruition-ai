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


if __name__ == "__main__":
    unittest.main()
