from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.modules.document_restoration.infrastructure import (
    process_auto_layout_blocks as process_module,
)
from app.modules.document_restoration.infrastructure.review_blocks_with_vision import (
    evaluate_result,
    is_rejected_result,
    normalize_vision_result,
)


class VisionRejectionTest(unittest.TestCase):
    def test_recognizes_rejection_with_or_without_brackets(self) -> None:
        self.assertTrue(is_rejected_result("[rejected: unreadable text]"))
        self.assertTrue(is_rejected_result("rejected: ..."))
        self.assertTrue(is_rejected_result("Rejected: no visible text"))
        self.assertFalse(is_rejected_result("Recovered text"))

    def test_does_not_wrap_unbracketed_equation_rejection_as_math(self) -> None:
        block = {"type": "equation_candidate"}

        result = normalize_vision_result(block, "rejected: unreadable equation")

        self.assertEqual(result, "rejected: unreadable equation")

    def test_rejects_unbracketed_text_rejection(self) -> None:
        block = {"type": "paragraph"}

        evaluation = evaluate_result(block, "rejected: ...")

        self.assertFalse(evaluation["accepted"])
        self.assertIn("vision 모델이 crop 검토를 거부함", evaluation["reasons"])

    def test_uses_docling_source_when_vision_recovery_is_rejected(self) -> None:
        block = {
            "id": "docling_text_p01_001",
            "type": "paragraph",
            "source_text": "Original Docling reference text.",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recovered_dir = root / "text_recovered"
            evaluation_dir = root / "text_evaluations"
            recovered_dir.mkdir()
            evaluation_dir.mkdir()
            (recovered_dir / "docling_text_p01_001.md").write_text(
                "rejected: ...\n",
                encoding="utf-8",
            )
            (evaluation_dir / "docling_text_p01_001.json").write_text(
                json.dumps({"accepted": False}),
                encoding="utf-8",
            )

            with (
                mock.patch.object(process_module, "TEXT_RECOVERED_DIR", recovered_dir),
                mock.patch.object(process_module, "TEXT_EVALUATION_DIR", evaluation_dir),
            ):
                result = process_module.block_source_text(block)

        self.assertEqual(result, "Original Docling reference text.")

    def test_preserves_recovered_heading_markdown(self) -> None:
        block = {
            "id": "docling_text_p01_001",
            "type": "heading",
            "source_text": "Original heading",
            "bbox": [1, 2, 3, 4],
            "confidence": "docling",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recovered_dir = root / "text_recovered"
            evaluation_dir = root / "text_evaluations"
            recovered_dir.mkdir()
            evaluation_dir.mkdir()
            (recovered_dir / "docling_text_p01_001.md").write_text(
                "## Restored heading\n",
                encoding="utf-8",
            )
            (evaluation_dir / "docling_text_p01_001.json").write_text(
                json.dumps({"accepted": True}),
                encoding="utf-8",
            )

            with (
                mock.patch.object(process_module, "TEXT_RECOVERED_DIR", recovered_dir),
                mock.patch.object(process_module, "TEXT_EVALUATION_DIR", evaluation_dir),
                mock.patch.object(process_module, "USE_RECOVERED_RESULTS", True),
            ):
                result = process_module.process_heading(block)

        self.assertEqual(result[-1], "## Restored heading")

    def test_can_ignore_stale_recovered_text_for_detected_markdown(self) -> None:
        block = {
            "id": "docling_text_p01_001",
            "type": "paragraph",
            "source_text": "Current Docling text.",
        }

        with mock.patch.object(
            process_module,
            "USE_RECOVERED_RESULTS",
            False,
        ):
            result = process_module.block_source_text(block)

        self.assertEqual(result, "Current Docling text.")

    def test_can_ignore_stale_layout_decision_for_detected_markdown(self) -> None:
        block = {
            "id": "docling_table_p01_001",
            "type": "table_candidate",
            "source_text": "Current table text.",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            evaluation_dir = Path(temp_dir)
            (evaluation_dir / "docling_table_p01_001.json").write_text(
                json.dumps(
                    {
                        "accepted": False,
                        "layout_decision": "discard_debris",
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(
                    process_module,
                    "EVALUATION_DIR",
                    evaluation_dir,
                ),
                mock.patch.object(
                    process_module,
                    "USE_RECOVERED_RESULTS",
                    False,
                ),
            ):
                self.assertFalse(
                    process_module.skip_layout_adjudicated_block(block)
                )
                self.assertEqual(process_module.layout_decision(block), "")
