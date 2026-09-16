import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz

from app.modules.document_restoration.infrastructure import crop_first_with_anydoc as module


class CropFirstWithAnyDocTest(unittest.TestCase):
    def test_parse_heron_regions_maps_types_and_tokens(self) -> None:
        regions = module.parse_heron_regions(
            "\n".join(
                [
                    "REGION\t0\t2\ttable\t0.91\t1\t2\t3\t4",
                    "REGION\t0\t2\tpicture\t0.82\t5\t6\t7\t8",
                    "REGION\t0\t3\tformula\t0.73\t9\t10\t11\t12",
                ]
            )
        )

        self.assertEqual(
            [
                (region["type"], region["page"], region["bbox"])
                for region in regions
            ],
            [
                ("table_candidate", 2, [1.0, 2.0, 3.0, 4.0]),
                ("figure_candidate", 2, [5.0, 6.0, 7.0, 8.0]),
                ("equation_candidate", 3, [9.0, 10.0, 11.0, 12.0]),
            ],
        )
        self.assertEqual(
            [region["token"] for region in regions],
            ["XQ001QX", "XQ002QX", "XQ003QX"],
        )

    def test_select_body_pages_prioritizes_broken_and_high_difference_pages(self) -> None:
        blocks = [
            {"page": 1, "body_broken": False, "body_difference": 0.1},
            {"page": 2, "body_broken": True, "body_difference": 0.2},
            {"page": 3, "body_broken": False, "body_difference": 0.9},
            {"page": 4, "body_broken": False, "body_difference": 0.8},
        ]

        module.select_body_pages(blocks, 0.5)

        self.assertEqual(
            [block["page"] for block in blocks if "text_decision" in block],
            [2, 3],
        )

    def test_assemble_restores_tables_and_equations_and_assembles_figure_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            manifest_file = output_dir / "manifest.json"
            output_file = output_dir / "final" / "restored.md"
            recovered_dir = output_dir / "layout" / "auto" / "recovered_blocks"
            recovered_dir.mkdir(parents=True)
            (recovered_dir / "table-1.md").write_text("| A |\n| --- |", encoding="utf-8")
            (recovered_dir / "equation-1.md").write_text("$$x=1$$", encoding="utf-8")
            figure = output_dir / "layout" / "crop_first" / "assets" / "figures" / "figure.png"
            figure.parent.mkdir(parents=True)
            figure.write_bytes(b"png")
            manifest_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "body-1",
                            "page": 1,
                            "order": 0,
                            "type": "paragraph",
                            "source_text": "before XQ001QX middle XQ002QX after XQ003QX",
                            "body_broken": False,
                        },
                        {
                            "id": "table-1",
                            "page": 1,
                            "order": 1,
                            "type": "table_candidate",
                            "token": "XQ001QX",
                            "asset": "layout/crop_first/assets/specials/table.png",
                        },
                        {
                            "id": "equation-1",
                            "page": 1,
                            "order": 2,
                            "type": "equation_candidate",
                            "token": "XQ002QX",
                            "asset": "layout/crop_first/assets/specials/equation.png",
                        },
                        {
                            "id": "figure-1",
                            "page": 1,
                            "order": 3,
                            "type": "figure_candidate",
                            "token": "XQ003QX",
                            "asset": "layout/crop_first/assets/figures/figure.png",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            module.assemble(manifest_file, output_dir, output_file)

            result = output_file.read_text(encoding="utf-8")
            self.assertIn("| A |\n| --- |", result)
            self.assertIn("$$x=1$$", result)
            self.assertIn(
                "![figure](../layout/crop_first/assets/figures/figure.png)",
                result,
            )
            self.assertNotIn("XQ", result)

    def test_assemble_falls_back_to_source_text_for_broken_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            manifest_file = output_dir / "manifest.json"
            output_file = output_dir / "final" / "restored.md"
            manifest_file.parent.mkdir(parents=True)
            manifest_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "body-1",
                            "page": 1,
                            "order": 0,
                            "type": "paragraph",
                            "source_text": "",
                            "fallback_text": "원본 본문",
                            "body_broken": True,
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            module.assemble(manifest_file, output_dir, output_file)

            self.assertEqual(
                output_file.read_text(encoding="utf-8"),
                "<!-- page 1 -->\n\n> 본문 자동 복원 실패\n\n원본 본문\n",
            )

    def test_unicode_text_and_ligatures_remain_safe(self) -> None:
        page = mock.Mock()
        page.get_text.return_value = [(0, 0, 0, 0, "ofﬁce", 0, 0, 0)]

        self.assertEqual(module.normalized_words("한국어 ofﬁce"), ["한국어", "office"])
        self.assertEqual(module.repair_ligatures("ofce", page), "office")

    def test_prepare_passes_each_body_page_pdf_to_anydoc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_file = root / "source.pdf"
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((72, 72), "body source")
                document.save(pdf_file)

            output_dir = root / "output"
            manifest_file = output_dir / "manifest.json"
            detected_markdown = output_dir / "detected.md"
            body_inputs: list[Path] = []

            def fake_run_anydoc(
                command: str,
                input_file: Path,
                output_file: Path,
                log_file: Path,
            ) -> int:
                body_inputs.append(input_file)
                output_file.write_text("AnyDoc body", encoding="utf-8")
                log_file.write_text("ok", encoding="utf-8")
                return 0

            args = argparse.Namespace(
                pdf_file=pdf_file,
                manifest_file=manifest_file,
                detected_markdown=detected_markdown,
                output_dir=output_dir,
                anydoc_command="anydoc",
                heron_command="heron",
                heron_model=None,
                pdfium_library=None,
                body_ai_budget=0.3,
            )
            with mock.patch.object(module, "run_heron", return_value=([], "")):
                with mock.patch.object(
                    module,
                    "run_anydoc",
                    side_effect=fake_run_anydoc,
                ):
                    module.prepare(args)

            self.assertEqual(len(body_inputs), 1)
            self.assertTrue(body_inputs[0].is_file())
            with fitz.open(body_inputs[0]) as body_document:
                self.assertIn("body source", body_document[0].get_text())
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            self.assertEqual(manifest[0]["source_text"], "AnyDoc body")
            self.assertEqual(manifest[0]["fallback_text"], "body source")

    def test_run_heron_propagates_detector_failure(self) -> None:
        failure = subprocess.CalledProcessError(
            17,
            ["heron", "input.pdf"],
            stderr="detector failed\n",
        )
        with mock.patch.object(module.subprocess, "run", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "Heron detector 실행 실패: detector failed"):
                module.run_heron(Path("input.pdf"), "heron", None, None)

    def test_assemble_replaces_duplicate_marker_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            manifest_file = output_dir / "manifest.json"
            output_file = output_dir / "final" / "restored.md"
            token = "XQ001QX"
            manifest_file.parent.mkdir(parents=True)
            manifest_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "body-1",
                            "page": 1,
                            "order": 0,
                            "type": "paragraph",
                            "source_text": f"before {token} {token} after",
                            "body_broken": False,
                        },
                        {
                            "id": "figure-1",
                            "page": 1,
                            "order": 1,
                            "type": "figure_candidate",
                            "token": token,
                            "asset": "layout/crop_first/assets/figures/figure.png",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            module.assemble(manifest_file, output_dir, output_file)
            first = output_file.read_text(encoding="utf-8")
            module.assemble(manifest_file, output_dir, output_file)
            second = output_file.read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertEqual(second.count("![figure]"), 1)
            self.assertNotIn(token, second)


if __name__ == "__main__":
    unittest.main()
