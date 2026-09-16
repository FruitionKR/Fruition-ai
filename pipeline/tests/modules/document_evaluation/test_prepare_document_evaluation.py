from __future__ import annotations

import unittest

from app.modules.document_evaluation.application.prepare_document_evaluation import prepare_document_evaluation
from app.modules.document_evaluation.infrastructure.assembled_markdown_parser import parse_assembled_markdown


MARKDOWN = """## Page 1
<!-- docling_text_p01_001 type=paragraph bbox=[1, 2, 3, 4] confidence=x -->
first

<!-- docling_text_p01_002 type=heading bbox=[5, 6, 7, 8] confidence=x -->
second
## Page 2
<!-- docling_text_p02_001 type=paragraph bbox=[9, 10, 11, 12] confidence=x -->
third
"""


class FakeEvaluator:
    def evaluate(self, job):
        return {"job_id": job.job_id, "called": True}


class PrepareDocumentEvaluationTest(unittest.TestCase):
    def test_prepares_pending_job_without_evaluator(self) -> None:
        blocks = parse_assembled_markdown(MARKDOWN)

        job, result = prepare_document_evaluation(
            markdown=MARKDOWN,
            pdf_reference="paper.pdf",
            blocks=blocks,
            max_blocks=2,
        )

        self.assertIsNone(result)
        self.assertEqual(len(job.chunks), 2)
        payload = job.to_dict()
        self.assertEqual(payload["status"], "pending_external_evaluator")
        self.assertEqual(payload["constraints"]["mandatory_crop_review_block_types"], ["equation_candidate"])
        self.assertEqual(payload["constraints"]["mandatory_crop_review_markers"], ["복원 필요"])
        self.assertEqual(job.chunks[0].blocks[1].markdown, "second")

    def test_uses_evaluator_when_provided(self) -> None:
        blocks = parse_assembled_markdown(MARKDOWN)

        job, result = prepare_document_evaluation(
            markdown=MARKDOWN,
            pdf_reference="paper.pdf",
            blocks=blocks,
            evaluator=FakeEvaluator(),
        )

        self.assertEqual(result, {"job_id": job.job_id, "called": True})


if __name__ == "__main__":
    unittest.main()
