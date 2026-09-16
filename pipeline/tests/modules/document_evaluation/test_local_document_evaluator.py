from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.modules.document_evaluation.interfaces import local_cli as LOCAL_CLI
from app.modules.document_evaluation.infrastructure import (
    local_document_evaluator as MODULE,
)

class EvaluateAssembledMarkdownTest(unittest.TestCase):
    def test_parse_and_chunk_preserve_block_boundaries(self) -> None:
        markdown = """## Page 1
<!-- docling_text_p01_001 type=paragraph bbox=[1, 2, 3, 4] confidence=x -->
first

<!-- docling_text_p01_002 type=heading bbox=[5, 6, 7, 8] confidence=x -->
second
## Page 2
<!-- docling_text_p02_001 type=paragraph bbox=[9, 10, 11, 12] confidence=x -->
third
"""
        blocks = MODULE.parse_blocks(markdown)
        chunks = MODULE.make_chunks(blocks, max_blocks=2, max_chars=100)

        self.assertEqual([block.id for block in blocks], ["docling_text_p01_001", "docling_text_p01_002", "docling_text_p02_001"])
        self.assertEqual(blocks[1].markdown, "second")
        self.assertEqual([len(chunk) for chunk in chunks], [2, 1])

    def test_vision_retries_only_uncertain_up_to_limit(self) -> None:
        block = MODULE.Block("docling_text_p01_001", "paragraph", 1, (1, 2, 3, 4), "text")
        statuses = iter(["uncertain", "uncertain", "corrected"])
        paddings = []

        with mock.patch.object(MODULE.time, "sleep"):
            result = MODULE.review_with_vision(
                block,
                "문장 파손",
                3,
                lambda _block, padding: paddings.append(padding) or b"png",
                lambda _prompt, _image: {"status": next(statuses), "transcription": "", "reason": ""},
            )

        self.assertEqual(paddings, [4.0, 8.0, 12.0])
        self.assertEqual(result["result"]["status"], "corrected")
        self.assertEqual(len(result["attempts"]), 3)

    def test_vision_stops_after_match(self) -> None:
        block = MODULE.Block("docling_text_p01_001", "paragraph", 1, (1, 2, 3, 4), "text")
        calls = []

        result = MODULE.review_with_vision(
            block,
            "확인",
            3,
            lambda _block, _padding: b"png",
            lambda _prompt, _image: calls.append(True) or {"status": "match", "transcription": "text", "reason": ""},
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["result"]["status"], "match")

    def test_apply_corrections_preserves_page_heading(self) -> None:
        markdown = """## Page 1
<!-- docling_text_p01_001 type=paragraph bbox=[1, 2, 3, 4] confidence=x -->
broken

## Page 2

<!-- docling_text_p02_001 type=paragraph bbox=[5, 6, 7, 8] confidence=x -->
kept
"""

        result = MODULE.apply_corrections(markdown, {"docling_text_p01_001": "corrected"})

        self.assertIn("corrected\n\n## Page 2", result)
        self.assertIn("docling_text_p02_001", result)
        self.assertNotIn("broken", result)

    def test_record_evaluation_timing_accumulates_resume_runs(self) -> None:
        report = {"evaluation_elapsed_seconds_total": 2.5}

        MODULE.record_evaluation_timing(
            report,
            evaluation_elapsed_seconds=1.25,
            restoration_elapsed_seconds=10.0,
        )

        self.assertEqual(report["evaluation_elapsed_seconds_last_run"], 1.25)
        self.assertEqual(report["evaluation_elapsed_seconds_total"], 3.75)
        self.assertEqual(report["restoration_elapsed_seconds"], 10.0)
        self.assertEqual(report["pdf_to_evaluated_processing_seconds"], 13.75)

    def test_read_restoration_elapsed_seconds_uses_restored_markdown_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final_dir = Path(temp_dir)
            markdown_file = final_dir / "paper.restored.md"
            timing_file = final_dir / "paper.pipeline_timing.json"
            timing_file.write_text('{"total_elapsed_seconds": 12.5}', encoding="utf-8")

            elapsed = MODULE.read_restoration_elapsed_seconds(markdown_file)

        self.assertEqual(elapsed, 12.5)

    def test_local_cli_records_evaluator_and_combined_processing_time(self) -> None:
        report = {"evaluation_elapsed_seconds_total": 1.0}
        argv = [
            "local_cli",
            "--markdown-file",
            "paper.restored.md",
            "--pdf-file",
            "paper.pdf",
            "--output-file",
            "paper.evaluation.json",
        ]

        with (
            mock.patch("sys.argv", argv),
            mock.patch.object(LOCAL_CLI, "evaluate", return_value=report),
            mock.patch.object(LOCAL_CLI, "write_artifacts") as write_artifacts,
            mock.patch.object(LOCAL_CLI, "read_restoration_elapsed_seconds", return_value=10.0),
            mock.patch.object(LOCAL_CLI.time, "perf_counter", side_effect=[20.0, 22.5]),
        ):
            LOCAL_CLI.main()

        final_report = write_artifacts.call_args_list[-1].args[1]
        self.assertEqual(write_artifacts.call_count, 1)
        self.assertEqual(final_report["evaluation_elapsed_seconds_last_run"], 2.5)
        self.assertEqual(final_report["evaluation_elapsed_seconds_total"], 3.5)
        self.assertEqual(final_report["pdf_to_evaluated_processing_seconds"], 13.5)

    def test_complete_decisions_fills_omitted_vision_result(self) -> None:
        final = {"decisions": []}
        vision_results = [
            {
                "block_id": "docling_text_p01_001",
                "result": {"status": "corrected", "transcription": "fixed"},
            }
        ]

        result = MODULE.complete_decisions(final, vision_results)

        self.assertEqual(result["decisions"][0]["decision"], "suggest_correction")
        self.assertEqual(result["decisions"][0]["suggested_markdown"], "fixed")

    def test_complete_decisions_normalizes_vision_status_as_decision(self) -> None:
        final = {"decisions": [{"block_id": "a", "decision": "corrected"}, {"block_id": "b", "decision": "match"}]}

        result = MODULE.complete_decisions(final, [])

        self.assertEqual([item["decision"] for item in result["decisions"]], ["suggest_correction", "keep"])

    def test_complete_decisions_keeps_only_vision_blocks_and_uses_transcription(self) -> None:
        final = {
            "decisions": [
                {"block_id": "table", "decision": "keep", "reason": "wrong", "suggested_markdown": "wrong"},
                {"block_id": "extra", "decision": "unresolved", "reason": "extra", "suggested_markdown": ""},
            ]
        }
        vision_results = [
            {
                "block_id": "table",
                "result": {"status": "corrected", "transcription": "| fixed |"},
            }
        ]

        result = MODULE.complete_decisions(final, vision_results)

        self.assertEqual(len(result["decisions"]), 1)
        self.assertEqual(result["decisions"][0]["decision"], "suggest_correction")
        self.assertEqual(result["decisions"][0]["suggested_markdown"], "| fixed |")

    def test_vision_tool_error_is_limited_by_max_attempts(self) -> None:
        block = MODULE.Block("docling_text_p01_001", "paragraph", 1, (1, 2, 3, 4), "text")

        with mock.patch.object(MODULE.time, "sleep"):
            result = MODULE.review_with_vision(
                block,
                "확인",
                2,
                lambda _block, _padding: b"png",
                lambda _prompt, _image: (_ for _ in ()).throw(RuntimeError("HTTP 500")),
            )

        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(result["result"]["status"], "uncertain")

    def test_evaluation_prompt_checks_broken_text_without_overflagging_terms(self) -> None:
        block = MODULE.Block("docling_text_p01_001", "paragraph", 1, (1, 2, 3, 4), "text")

        prompt = MODULE.evaluation_prompt([block])

        self.assertIn("mojibake", prompt)
        self.assertIn("inconsistent mixed scripts", prompt)
        self.assertIn("character fragments", prompt)
        self.assertIn("technical term", prompt)
        self.assertIn("Optimize for precision", prompt)
        self.assertIn("hyphenated word", prompt)
        self.assertIn("Quote the exact suspicious substring", prompt)

    def test_table_prompts_require_row_and_column_structure_review(self) -> None:
        block = MODULE.Block("docling_table_p01_001", "table_candidate", 1, (1, 2, 3, 4), "| A | B |")

        evaluation = MODULE.evaluation_prompt([block])
        vision = MODULE.vision_prompt(
            block,
            "표 구조 이상",
            1,
            [{"x": 1.0, "y": 2.0, "text": "A"}],
            2,
        )

        self.assertIn("inconsistent cell", evaluation)
        self.assertIn("generic `Column N` headers", evaluation)
        self.assertIn("observed row/column defect", evaluation)
        self.assertIn("Hangul Korean, not Chinese", evaluation)
        self.assertIn("row-to-column relationships", vision)
        self.assertIn("Markdown table", vision)
        self.assertIn("subscripts", vision)
        self.assertIn("content is cut at a crop boundary", vision)
        self.assertIn("never replace a block with a visible subset", vision)
        self.assertIn("known OCR table is deliberately omitted", vision)
        self.assertIn("one explicit header per data column", vision)
        self.assertIn("source_columns", vision)
        self.assertIn("exact strings as the Markdown header cells", vision)
        self.assertIn("left edge to its right edge", vision)
        self.assertIn("positioned source words", vision)
        self.assertIn("corresponding distinct cells", vision)
        self.assertIn("Positioned words extracted from the same source crop", vision)
        self.assertIn("text-layout detector found 2 vertical columns", vision)

    def test_equation_candidate_is_always_reviewed_against_source_crop(self) -> None:
        block = MODULE.Block(
            "docling_equation_p01_001",
            "equation_candidate",
            1,
            (1, 2, 3, 4),
            "$$a = \\frac{b}{c}$$",
        )
        args = SimpleNamespace(pdf_file=Path("input.pdf"), max_blocks=12, max_chars=6000, max_chunks=0)

        plan = MODULE.build_evaluation_plan([block], args)
        prompt = MODULE.vision_prompt(block, "[equation_fidelity] 원본 수식 대조", 1)

        self.assertEqual(plan.local_blocks, [block])
        self.assertIn("every visible equation row", prompt)
        self.assertIn("coefficients, operators, denominators", prompt)
        self.assertIn("exponents, subscripts", prompt)
        self.assertIn(block.markdown, prompt)

    def test_select_requests_accepts_equation_fidelity_review(self) -> None:
        block = MODULE.Block(
            "docling_equation_p01_001",
            "equation_candidate",
            1,
            (1, 2, 3, 4),
            "$$a=b$$",
        )

        requests = MODULE.select_requests(
            {
                "requests": [
                    {
                        "block_id": block.id,
                        "reason": "[equation_fidelity] 분모 누락 가능성",
                    }
                ]
            },
            [block],
        )

        self.assertEqual(requests[0]["block_id"], block.id)

    def test_broken_text_vision_prompt_omits_corrupted_ocr(self) -> None:
        corrupted = "known corrupted OCR"
        block = MODULE.Block("docling_text_p01_001", "paragraph", 1, (1, 2, 3, 4), corrupted)

        vision = MODULE.vision_prompt(block, f"[broken_text] {corrupted}", 1)

        self.assertNotIn(corrupted, vision)
        self.assertIn("independently from the image", vision)

    def test_table_vision_retries_when_declared_columns_do_not_match_header(self) -> None:
        block = MODULE.Block("docling_table_p01_001", "table_candidate", 1, (1, 2, 3, 4), "broken")
        answers = iter(
            [
                {
                    "status": "corrected",
                    "source_columns": ["A", "B", "C"],
                    "transcription": "| A | B |\n| --- | --- |\n| 1 | 2 |",
                    "reason": "mismatch",
                },
                {
                    "status": "corrected",
                    "source_columns": ["A", "B"],
                    "transcription": "| A | B |\n| --- | --- |\n| 1 | 2 |",
                    "reason": "consistent",
                },
            ]
        )

        with mock.patch.object(MODULE.time, "sleep"):
            result = MODULE.review_with_vision(
                block,
                "[table_structure] invalid",
                2,
                lambda _block, _padding: b"png",
                lambda _prompt, _image: next(answers),
            )

        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(result["result"]["source_columns"], ["A", "B"])

    def test_table_transcription_requires_pure_consistent_markdown_table(self) -> None:
        valid = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        failure_prefix = "> table recovery failed\n\n" + valid
        inconsistent = "| A | B |\n| --- | --- |\n| 1 | 2 | 3 |"

        self.assertTrue(MODULE.is_valid_markdown_table(valid))
        self.assertFalse(MODULE.is_valid_markdown_table(failure_prefix))
        self.assertFalse(MODULE.is_valid_markdown_table(inconsistent))

    def test_table_result_requires_declared_columns_to_equal_markdown_header(self) -> None:
        markdown = "| A | B |\n| --- | --- |\n| 1 | 2 |"

        self.assertTrue(
            MODULE.is_valid_table_result(
                {"source_columns": ["A", "B"], "transcription": markdown}
            )
        )
        self.assertFalse(
            MODULE.is_valid_table_result(
                {"source_columns": ["A", "B", "C"], "transcription": markdown}
            )
        )
        self.assertFalse(
            MODULE.is_valid_table_result(
                {"source_columns": ["A", "B"], "transcription": markdown},
                detected_column_count=3,
            )
        )

    def test_table_result_preserves_values_from_source_crop(self) -> None:
        positioned_words = [
            {"x": 0.0, "y": 1.0, "text": "Maximum"},
            {"x": 0.5, "y": 1.0, "text": "Voltage"},
            {"x": 1.0, "y": 1.0, "text": "12.5"},
            {"x": 2.0, "y": 1.0, "text": "-3"},
            {"x": 3.0, "y": 1.0, "text": "±0.5"},
        ]

        self.assertTrue(
            MODULE.is_valid_table_result(
                {
                    "source_columns": ["A", "B"],
                    "transcription": "| A | B |\n| --- | --- |\n| Maximum Voltage 12.5 | -3 ±0.5 |",
                },
                positioned_words=positioned_words,
            )
        )
        self.assertFalse(
            MODULE.is_valid_table_result(
                {
                    "source_columns": ["A", "B"],
                    "transcription": "| A | B |\n| --- | --- |\n| Voltage 12.5 | -3 ±0.5 |",
                },
                positioned_words=positioned_words,
            )
        )
        self.assertFalse(
            MODULE.is_valid_table_result(
                {
                    "source_columns": ["A", "B"],
                    "transcription": "| A | B |\n| --- | --- |\n| 12 | -3 ±0.5 |",
                },
                positioned_words=positioned_words,
            )
        )
        self.assertFalse(
            MODULE.is_valid_table_result(
                {
                    "source_columns": ["A", "B"],
                    "transcription": "| A | B |\n| --- | --- |\n| Maximum Voltage 12.5 | -3 0.5 |",
                },
                positioned_words=positioned_words,
            )
        )

    def test_table_result_preserves_source_cell_positions(self) -> None:
        positioned_words = [
            {"x": 0.0, "y": 0.0, "text": "Parameter"},
            {"x": 100.0, "y": 0.0, "text": "Unit"},
            {"x": 200.0, "y": 0.0, "text": "Initial"},
            {"x": 300.0, "y": 0.0, "text": "Optimised"},
            {"x": 0.0, "y": 20.0, "text": "PM1ro"},
            {"x": 100.0, "y": 20.0, "text": "mm"},
            {"x": 200.0, "y": 20.0, "text": "102"},
            {"x": 300.0, "y": 20.0, "text": "102"},
        ]
        evidence = MODULE.build_table_source_evidence(positioned_words, 4)

        self.assertFalse(
            MODULE.is_valid_table_result(
                {
                    "source_columns": ["Parameter", "Unit", "Initial", "Optimised"],
                    "transcription": (
                        "| Parameter | Unit | Initial | Optimised |\n"
                        "| --- | --- | --- | --- |\n"
                        "| PM1ro | mm | 102 102 |  |"
                    ),
                },
                detected_column_count=4,
                source_evidence=evidence,
            )
        )

        single_letter_evidence = MODULE.build_table_source_evidence(
            [
                {"x": 0.0, "y": 0.0, "text": "X"},
                {"x": 0.0, "y": 20.0, "text": "1"},
            ],
            1,
        )
        self.assertTrue(
            MODULE.is_valid_table_result(
                {
                    "source_columns": ["X"],
                    "transcription": "| X |\n| --- |\n| 1 |",
                },
                detected_column_count=1,
                source_evidence=single_letter_evidence,
            )
        )
        self.assertFalse(
            MODULE.is_valid_table_result(
                {
                    "source_columns": ["X"],
                    "transcription": "| X |\n| --- |\n| 1 |\n| 1 |",
                },
                detected_column_count=1,
                source_evidence=single_letter_evidence,
            )
        )
        self.assertFalse(
            MODULE.is_valid_table_result(
                {
                    "source_columns": ["Parameter", "Unit", "Initial", "Optimised"],
                    "transcription": (
                        "| Parameter | Unit | Initial | Optimised |\n"
                        "| --- | --- | --- | --- |\n"
                        "| PM1ro | mm | 102 | 102 |\n"
                        "| PM1ro | mm | 102 | 102 |"
                    ),
                },
                detected_column_count=4,
                source_evidence=evidence,
            )
        )

    def test_table_result_preserves_image_only_plus_minus_evidence(self) -> None:
        positioned_words = [
            {"x": 0.0, "y": 0.0, "text": "Noise"},
            {"x": 0.0, "y": 20.0, "text": "0.02"},
        ]
        ocr_words = [{"x": 0.0, "y": 20.0, "text": "+£0.02"}]
        evidence = MODULE.build_table_source_evidence(positioned_words, 1, ocr_words)

        self.assertFalse(
            MODULE.is_valid_table_result(
                {
                    "source_columns": ["Noise"],
                    "transcription": "| Noise |\n| --- |\n| 0.02 |",
                },
                detected_column_count=1,
                source_evidence=evidence,
            )
        )
        self.assertTrue(
            MODULE.is_valid_table_result(
                {
                    "source_columns": ["Noise"],
                    "transcription": "| Noise |\n| --- |\n| ±0.02 |",
                },
                detected_column_count=1,
                source_evidence=evidence,
            )
        )
        self.assertTrue(
            MODULE.is_valid_table_result(
                {
                    "source_columns": ["Noise"],
                    "transcription": "| Noise |\n| --- |\n| $\\pm 0.02$ |",
                },
                detected_column_count=1,
                source_evidence=evidence,
            )
        )

    def test_table_ocr_reports_execution_failure(self) -> None:
        with mock.patch.object(MODULE.subprocess, "run", side_effect=OSError):
            result = MODULE.extract_table_ocr_words(b"png")

        self.assertFalse(result.succeeded)
        self.assertEqual(result.words, [])

    def test_table_match_rejects_current_markdown_with_missing_source_value(self) -> None:
        block = MODULE.Block(
            "docling_table_p01_001",
            "table_candidate",
            1,
            (1, 2, 3, 4),
            "| Name | Value |\n| --- | --- |\n| Maximum Voltage | 12 |",
        )

        result = MODULE.review_with_vision(
            block,
            "[table_structure] 원본 값 대조",
            1,
            lambda _block, _padding: b"png",
            lambda _prompt, _image: {
                "status": "match",
                "transcription": "",
                "reason": "현재 표와 원본이 같음",
            },
            [
                {"text": "Maximum"},
                {"text": "Voltage"},
                {"text": "12.5"},
            ],
            2,
        )

        self.assertEqual(result["result"]["status"], "uncertain")

    def test_table_match_rejects_invalid_current_markdown_table(self) -> None:
        block = MODULE.Block(
            "docling_table_p01_001",
            "table_candidate",
            1,
            (1, 2, 3, 4),
            "| Name | Value |\n| broken | delimiter |\n| Maximum Voltage | 12.5 |",
        )

        result = MODULE.review_with_vision(
            block,
            "[table_structure] 깨진 delimiter",
            1,
            lambda _block, _padding: b"png",
            lambda _prompt, _image: {
                "status": "match",
                "transcription": "",
                "reason": "현재 표와 원본이 같음",
            },
            [
                {"text": "Maximum"},
                {"text": "Voltage"},
                {"text": "12.5"},
            ],
            2,
        )

        self.assertEqual(result["result"]["status"], "uncertain")

    def test_table_match_rejects_missing_plus_minus_sign(self) -> None:
        block = MODULE.Block(
            "docling_table_p01_001",
            "table_candidate",
            1,
            (1, 2, 3, 4),
            "| Name | Value |\n| --- | --- |\n| Noise | 0.5 |",
        )

        result = MODULE.review_with_vision(
            block,
            "[table_structure] 원본 부호 대조",
            1,
            lambda _block, _padding: b"png",
            lambda _prompt, _image: {
                "status": "match",
                "transcription": "",
                "reason": "현재 표와 원본이 같음",
            },
            [
                {"text": "Noise"},
                {"text": "±0.5"},
            ],
            2,
        )

        self.assertEqual(result["result"]["status"], "uncertain")

    def test_assemble_markdown_table_handles_single_header(self) -> None:
        grid = [
            ["Name", "Value"],
            ["First", "1"],
            ["Second", "2"],
        ]

        result = MODULE.assemble_markdown_table(grid)

        self.assertEqual(
            result,
            "| Name | Value |\n| --- | --- |\n| First | 1 |\n| Second | 2 |",
        )

    def test_assemble_markdown_table_expands_hierarchical_headers(self) -> None:
        grid = [
            ["", "", "Group", "One", "", "", "Group", "Two"],
            ["Name", "Code", "", "", "", "", "", ""],
            ["", "", "X", "Y", "Z", "X", "Y", "Z"],
            ["First", "A", "1", "2", "3", "4", "5", "6"],
            ["Second", "B", "7", "8", "9", "10", "11", "12"],
        ]

        result = MODULE.assemble_markdown_table(grid)

        self.assertIsNotNone(result)
        self.assertIn("Group One / X", result)
        self.assertIn("Group Two / Z", result)
        self.assertTrue(MODULE.is_valid_markdown_table(result or ""))

    def test_evaluate_applies_vision_result_without_second_text_call(self) -> None:
        markdown = """<!-- docling_text_p01_001 type=paragraph bbox=[1, 2, 3, 4] confidence=x -->
mixedCaseTOKENLLLLfragment
"""
        calls = []

        def fake_call_model(_endpoint, _model, _prompt, image=None):
            calls.append("vision" if image else "text")
            return {"status": "corrected", "transcription": "fixed", "reason": "복원됨"}

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            markdown_file = temp_path / "input.md"
            markdown_file.write_text(markdown, encoding="utf-8")
            args = SimpleNamespace(
                markdown_file=markdown_file,
                pdf_file=temp_path / "input.pdf",
                max_blocks=12,
                max_chars=6000,
                max_chunks=0,
                resume=False,
                output_file=temp_path / "output.json",
                dry_run=False,
                max_vision_requests=0,
                endpoint="http://localhost",
                evaluator_model="text-model",
                vision_model="vision-model",
                max_vision_attempts=2,
            )
            with (
                mock.patch.object(MODULE, "call_model", side_effect=fake_call_model),
                mock.patch.object(MODULE, "render_crop", return_value=b"png"),
                mock.patch.object(MODULE, "write_artifacts"),
            ):
                report = MODULE.evaluate(args)

        self.assertEqual(calls, ["vision"])
        self.assertEqual(report["text_evaluator_call_count"], 0)
        decision = report["chunks"][0]["final_evaluation"]["decisions"][0]
        self.assertEqual(decision["decision"], "suggest_correction")
        self.assertEqual(decision["suggested_markdown"], "fixed")

    def test_evaluate_rechecks_text_layout_table_when_ocr_fails(self) -> None:
        markdown = """<!-- docling_table_p01_001 type=table_candidate bbox=[1, 2, 3, 4] confidence=x -->
| Parameter | Unit | Initial | Optimised |
| broken | delimiter |
| PM1ro | mm | 102 102 | |
"""
        corrected = (
            "| Parameter | Unit | Initial | Optimised |\n"
            "| --- | --- | --- | --- |\n"
            "| PM1ro | mm | 102 | 102 |"
        )
        positioned_words = [
            {"x": 0.0, "y": 0.0, "text": "Parameter"},
            {"x": 100.0, "y": 0.0, "text": "Unit"},
            {"x": 200.0, "y": 0.0, "text": "Initial"},
            {"x": 300.0, "y": 0.0, "text": "Optimised"},
            {"x": 0.0, "y": 20.0, "text": "PM1ro"},
            {"x": 100.0, "y": 20.0, "text": "mm"},
            {"x": 200.0, "y": 20.0, "text": "102"},
            {"x": 300.0, "y": 20.0, "text": "102"},
        ]
        prompts = []

        def fake_call_model(_endpoint, _model, prompt, image=None):
            prompts.append(prompt)
            return {
                "status": "corrected",
                "source_columns": ["Parameter", "Unit", "Initial", "Optimised"],
                "transcription": corrected,
                "reason": "cell 위치를 복원함",
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            markdown_file = temp_path / "input.md"
            markdown_file.write_text(markdown, encoding="utf-8")
            args = SimpleNamespace(
                markdown_file=markdown_file,
                pdf_file=temp_path / "input.pdf",
                max_blocks=12,
                max_chars=6000,
                max_chunks=0,
                resume=False,
                output_file=temp_path / "output.json",
                dry_run=False,
                max_vision_requests=0,
                endpoint="http://localhost",
                evaluator_model="text-model",
                vision_model="vision-model",
                max_vision_attempts=2,
            )
            with (
                mock.patch.object(MODULE, "restore_table_from_text_layout", return_value=corrected),
                mock.patch.object(MODULE, "detect_table_column_count", return_value=4),
                mock.patch.object(MODULE, "extract_positioned_words", return_value=positioned_words),
                mock.patch.object(
                    MODULE,
                    "extract_table_ocr_words",
                    return_value=MODULE.TableOcrResult(words=[], succeeded=False),
                ),
                mock.patch.object(MODULE, "render_crop", return_value=b"png"),
                mock.patch.object(MODULE, "call_model", side_effect=fake_call_model),
                mock.patch.object(MODULE, "write_artifacts"),
            ):
                report = MODULE.evaluate(args)

        decision = report["chunks"][0]["final_evaluation"]["decisions"][0]
        self.assertEqual(decision["suggested_markdown"], corrected)
        self.assertIn("Tesseract TSV", prompts[0])

    def test_text_fallback_only_includes_ambiguous_tables(self) -> None:
        valid_table = MODULE.Block(
            "docling_table_p01_001",
            "table_candidate",
            1,
            (1, 2, 3, 4),
            "| Column 1 | Column 2 |\n| --- | --- |\n| 1 | 2 |",
        )
        incomplete = MODULE.Block(
            "docling_text_p01_002",
            "paragraph",
            1,
            (1, 2, 3, 4),
            "This otherwise normal paragraph unexpectedly ends with a",
        )
        normal = MODULE.Block(
            "docling_text_p01_003",
            "paragraph",
            1,
            (1, 2, 3, 4),
            "This is a complete technical sentence.",
        )

        self.assertTrue(MODULE.needs_text_fallback(valid_table, detected_column_count=2))
        self.assertFalse(MODULE.needs_text_fallback(incomplete))
        self.assertFalse(MODULE.needs_text_fallback(normal))

    def test_builds_evaluation_plan_before_model_execution(self) -> None:
        table = MODULE.Block(
            "docling_table_p01_001",
            "table_candidate",
            1,
            (1, 2, 3, 4),
            "| Column 1 | Column 2 |\n| --- | --- |\n| 1 | 2 |",
        )
        normal = MODULE.Block(
            "docling_text_p01_002",
            "paragraph",
            1,
            (1, 2, 3, 4),
            "This is a complete technical sentence.",
        )
        args = SimpleNamespace(pdf_file=Path("input.pdf"), max_blocks=12, max_chars=6000, max_chunks=0)

        with (
            mock.patch.object(MODULE, "restore_table_from_text_layout", return_value=None),
            mock.patch.object(MODULE, "detect_table_column_count", return_value=2),
        ):
            plan = MODULE.build_evaluation_plan([table, normal], args)

        self.assertEqual([block.id for block in plan.local_blocks], [table.id])
        self.assertEqual(plan.fallback_blocks, [])
        self.assertEqual(plan.selected_batches, [("local", [table])])

    def test_evaluate_sends_ambiguous_table_directly_to_vision(self) -> None:
        markdown = """<!-- docling_table_p01_001 type=table_candidate bbox=[1, 2, 3, 4] confidence=x -->
| Column 1 | Column 2 |
| --- | --- |
| 1 | 2 |
"""
        calls = []

        def fake_call_model(_endpoint, _model, _prompt, image=None):
            calls.append("vision" if image else "text")
            return {
                "status": "corrected",
                "transcription": "| Name | Value |\n| --- | --- |\n| A | 1 |",
                "reason": "복원됨",
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            markdown_file = temp_path / "input.md"
            markdown_file.write_text(markdown, encoding="utf-8")
            args = SimpleNamespace(
                markdown_file=markdown_file,
                pdf_file=temp_path / "input.pdf",
                max_blocks=12,
                max_chars=6000,
                max_chunks=0,
                resume=False,
                output_file=temp_path / "output.json",
                dry_run=False,
                max_vision_requests=0,
                endpoint="http://localhost",
                evaluator_model="text-model",
                vision_model="vision-model",
                max_vision_attempts=2,
            )
            with (
                mock.patch.object(MODULE, "call_model", side_effect=fake_call_model),
                mock.patch.object(MODULE, "render_crop", return_value=b"png"),
                mock.patch.object(MODULE, "restore_table_from_text_layout", return_value=None),
                mock.patch.object(MODULE, "detect_table_column_count", return_value=2),
                mock.patch.object(MODULE, "extract_positioned_words", return_value=[]),
                mock.patch.object(MODULE.time, "sleep"),
                mock.patch.object(MODULE, "write_artifacts"),
            ):
                report = MODULE.evaluate(args)

        self.assertTrue(calls)
        self.assertNotIn("text", calls)
        self.assertEqual(report["local_candidate_count"], 1)
        self.assertEqual(report["fallback_candidate_count"], 0)
        self.assertEqual(report["text_evaluator_call_count"], 0)

    def test_text_fallback_rejects_equivalent_or_lower_quality_table_layout(self) -> None:
        current = MODULE.Block(
            "docling_table_p01_001",
            "table_candidate",
            1,
            (1, 2, 3, 4),
            "| Parameter | Value |\n| --- | --- |\n| gap | 1.0 |",
        )
        equivalent = "| Parameter | Value |\n| --- | --- |\n| gap | 1.0 |"
        degraded = "| 3DUDPHWHU | 9DOXH |\n| --- | --- |\n| JDS |  |"

        self.assertFalse(MODULE.needs_text_fallback(current, equivalent, 2))
        self.assertFalse(MODULE.needs_text_fallback(current, degraded, 2))

    def test_strong_table_layout_evidence_requires_materially_better_layout(self) -> None:
        placeholder = MODULE.Block(
            "docling_table_p01_001",
            "table_candidate",
            1,
            (1, 2, 3, 4),
            "| Column 1 | Column 2 |\n| --- | --- |\n| A B | 1 2 |",
        )
        restored = "| Name | Value |\n| --- | --- |\n| A | 1 |\n| B | 2 |"
        normal = MODULE.Block(
            "docling_table_p01_002",
            "table_candidate",
            1,
            (1, 2, 3, 4),
            "| Name | Value |\n| --- | --- |\n| A | 1 |\n| B | 2 |",
        )

        self.assertTrue(MODULE.has_strong_table_layout_evidence(placeholder, restored, 2))
        self.assertFalse(MODULE.has_strong_table_layout_evidence(normal, restored, 2))
        self.assertTrue(
            MODULE.has_strong_table_layout_evidence(
                normal,
                "| Name | Value |\n| --- | --- |\n| Alpha | 10 |\n| Beta | 20 |",
                2,
            )
        )

    def test_local_review_handles_broken_text_and_invalid_table(self) -> None:
        broken_text = MODULE.Block(
            "docling_text_p01_001",
            "paragraph",
            1,
            (1, 2, 3, 4),
            "mixedCaseTOKENLLLLfragment",
        )
        invalid_table = MODULE.Block(
            "docling_table_p01_001",
            "table_candidate",
            1,
            (1, 2, 3, 4),
            "flattened table content",
        )

        self.assertTrue(MODULE.needs_local_review(broken_text))
        self.assertTrue(MODULE.needs_local_review(invalid_table))
        self.assertFalse(MODULE.needs_text_fallback(invalid_table))

    def test_local_review_preserves_page_boundary_sentence_ending(self) -> None:
        incomplete = MODULE.Block(
            "docling_text_p01_001",
            "paragraph",
            1,
            (1, 2, 3, 4),
            "This long technical paragraph is visibly cut after an article a",
        )
        complete = MODULE.Block(
            "docling_text_p01_002",
            "paragraph",
            1,
            (1, 2, 3, 4),
            "This long technical paragraph ends with a complete statement.",
        )

        self.assertFalse(MODULE.needs_local_review(incomplete))
        self.assertFalse(MODULE.needs_local_review(complete))
        self.assertEqual(MODULE.select_requests({"requests": []}, [incomplete]), [])

    def test_select_requests_accepts_evaluator_broken_text_without_content_rules(self) -> None:
        block = MODULE.Block("docling_text_p01_001", "paragraph", 1, (1, 2, 3, 4), "input text")
        initial = {"requests": [{"block_id": block.id, "reason": "[broken_text] visible corruption"}]}

        self.assertEqual(MODULE.select_requests(initial, [block]), initial["requests"])

    def test_select_requests_adds_suspicious_text_as_vision_candidate(self) -> None:
        block = MODULE.Block(
            "docling_text_p01_001",
            "paragraph",
            1,
            (1, 2, 3, 4),
            "mixedCaseTOKENLLLLfragment",
        )

        requests = MODULE.select_requests({"requests": []}, [block])

        self.assertEqual(requests[0]["block_id"], block.id)
        self.assertTrue(requests[0]["reason"].startswith("[broken_text]"))

    def test_suspicious_text_pattern_does_not_flag_normal_citations(self) -> None:
        text = "The method is described in [5], [6], and [7]."

        self.assertFalse(MODULE.has_suspicious_text_pattern(text))

    def test_select_requests_accepts_table_structure_only_for_table(self) -> None:
        table = MODULE.Block("docling_table_p01_001", "table_candidate", 1, (1, 2, 3, 4), "| Column 1 |")
        text = MODULE.Block("docling_text_p01_002", "paragraph", 1, (1, 2, 3, 4), "normal")
        initial = {
            "requests": [
                {"block_id": table.id, "reason": "[table_structure] broken"},
                {"block_id": text.id, "reason": "[table_structure] broken"},
            ]
        }

        self.assertEqual(MODULE.select_requests(initial, [table, text]), [initial["requests"][0]])

    def test_select_requests_adds_invalid_table_when_evaluator_misclassifies_it(self) -> None:
        table = MODULE.Block(
            "docling_table_p01_001",
            "table_candidate",
            1,
            (1, 2, 3, 4),
            "flattened table content",
        )
        initial = {"requests": [{"block_id": table.id, "reason": "[broken_text] flattened"}]}

        requests = MODULE.select_requests(initial, [table])

        self.assertEqual(requests[0]["block_id"], table.id)
        self.assertTrue(requests[0]["reason"].startswith("[table_structure]"))


if __name__ == "__main__":
    unittest.main()
