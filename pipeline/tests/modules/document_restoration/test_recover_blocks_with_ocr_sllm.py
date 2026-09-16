from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.modules.document_restoration.infrastructure import (
    recover_blocks_with_ocr_sllm as module,
)
from app.modules.document_restoration.infrastructure.recover_blocks_with_ocr_sllm import (
    resolve_paddle_cache_dir,
    should_use_sllm,
)


class ResolvePaddleCacheDirTest(unittest.TestCase):
    def test_uses_nearest_parent_for_shallow_output_path(self) -> None:
        self.assertEqual(
            resolve_paddle_cache_dir(Path("/tmp/output")),
            Path("/tmp/paddle_cache"),
        )

    def test_handles_root_output_path(self) -> None:
        self.assertEqual(resolve_paddle_cache_dir(Path("/")), Path("/paddle_cache"))


class SllmRoutingTest(unittest.TestCase):
    def test_skips_sllm_for_table_candidates(self) -> None:
        self.assertFalse(should_use_sllm("table_candidate", enabled=True))

    def test_uses_sllm_for_equation_candidates_when_enabled(self) -> None:
        self.assertTrue(should_use_sllm("equation_candidate", enabled=True))

    def test_skips_sllm_for_equations_when_disabled(self) -> None:
        self.assertFalse(should_use_sllm("equation_candidate", enabled=False))

    def test_unresolved_table_is_left_for_vision_without_sllm_call(self) -> None:
        block = {
            "id": "docling_table_p01_001",
            "type": "table_candidate",
            "asset": "table.png",
            "source_text": "",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "recovered_blocks"
            ocr_dir = root / "ocr"
            evaluation_dir = root / "evaluations"
            ocr_dir.mkdir()
            with (
                mock.patch.object(module, "OUTPUT_DIR", output_dir),
                mock.patch.object(module, "OCR_DIR", ocr_dir),
                mock.patch.object(module, "EVALUATION_DIR", evaluation_dir),
                mock.patch.object(module, "ocr_image", return_value="unresolved table"),
                mock.patch.object(module, "prompt_for_block") as prompt_for_block,
                mock.patch.object(module, "structured_table_markdown", return_value=None),
                mock.patch.object(
                    module,
                    "evaluate_block",
                    return_value={"accepted": False, "score": 0.0, "reasons": ["invalid"]},
                ) as evaluate_block,
                mock.patch.object(module, "call_sllm") as call_sllm,
            ):
                module.recover_block(block, "endpoint", "model", use_sllm=True)

            call_sllm.assert_not_called()
            prompt_for_block.assert_not_called()
            self.assertFalse((output_dir / "docling_table_p01_001.prompt.md").exists())
            self.assertFalse(evaluate_block.call_args.args[-1])
            evaluation = json.loads(
                (evaluation_dir / "docling_table_p01_001.json").read_text(encoding="utf-8")
            )
            self.assertFalse(evaluation["accepted"])


class DeterministicEvaluationTest(unittest.TestCase):
    def test_collects_table_specific_reasons(self) -> None:
        result = module.deterministic_evaluation(
            {"type": "table_candidate"},
            "| 1 | 2 |\n| --- | --- |\n| 3 | 4 |",
        )

        self.assertEqual(result["reasons"], ["table header에 데이터 값이 들어감"])

    def test_accepts_well_formed_equation(self) -> None:
        result = module.deterministic_evaluation(
            {"type": "equation_candidate"},
            "$$ x = 1 $$",
        )

        self.assertEqual(result, {"accepted": True, "score": 1.0, "reasons": []})

    def test_keeps_common_rejection_reason_before_type_reasons(self) -> None:
        result = module.deterministic_evaluation(
            {"type": "figure_candidate"},
            "[rejected: unreadable]",
        )

        self.assertEqual(
            result["reasons"],
            ["generator가 복원을 거부함", "figure block은 Vision crop 검토가 필요함"],
        )


class RecoveryStageTest(unittest.TestCase):
    def test_uses_accepted_structured_table_candidate(self) -> None:
        block = {"type": "table_candidate"}
        with (
            mock.patch.object(module, "structured_table_markdown", return_value="| A |\n| --- |"),
            mock.patch.object(
                module,
                "deterministic_evaluation",
                return_value={"accepted": True, "score": 1.0, "reasons": []},
            ),
        ):
            markdown, evaluation = module.deterministic_recovery_candidate(block, "ocr")

        self.assertEqual(markdown, "| A |\n| --- |")
        self.assertEqual(evaluation["recovery_source"], "structured_table_parser")

    def test_uses_equation_specific_system_message(self) -> None:
        message = module.sllm_system_message("equation_candidate")

        self.assertIn("OCR-to-LaTeX", message)
        self.assertIn("$$", message)
