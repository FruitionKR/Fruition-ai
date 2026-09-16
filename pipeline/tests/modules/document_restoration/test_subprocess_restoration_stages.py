import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.modules.document_restoration.application.models import (
    PreparedRestoration,
    RestoreDocumentCommand,
)
from app.modules.document_restoration.domain.entities import RestorationStage
from app.modules.document_restoration.infrastructure.subprocess_restoration_stages import (
    SubprocessDocumentRestorationStages,
)


class SubprocessDocumentRestorationStagesTest(unittest.TestCase):
    def test_rejects_incomplete_cached_docling_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_file = root / "source.pdf"
            docling_json = root / "source.json"
            pdf_file.write_bytes(b"pdf")
            docling_json.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "--docling-json.*--docling-markdown",
            ):
                SubprocessDocumentRestorationStages().prepare(
                    RestoreDocumentCommand(
                        pdf_file=pdf_file,
                        docling_json=docling_json,
                        output_dir=root / "output",
                        document_slug="paper",
                    )
                )

    def test_prepare_copies_inputs_to_canonical_output_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_file = root / "source.pdf"
            docling_json = root / "source.json"
            docling_markdown = root / "source.md"
            pdf_file.write_bytes(b"pdf")
            docling_json.write_text("{}", encoding="utf-8")
            docling_markdown.write_text("# source", encoding="utf-8")
            output_dir = root / "output"

            prepared = SubprocessDocumentRestorationStages().prepare(
                RestoreDocumentCommand(
                    pdf_file=pdf_file,
                    docling_json=docling_json,
                    docling_markdown=docling_markdown,
                    output_dir=output_dir,
                    document_slug="paper",
                )
            )

            self.assertEqual(prepared.pdf_file, output_dir / "paper.pdf")
            self.assertEqual(
                prepared.docling_json,
                output_dir / "layout" / "auto" / "docling_ocr_baseline" / "docling.json",
            )
            self.assertEqual(
                prepared.docling_markdown,
                output_dir / "layout" / "auto" / "docling_ocr_baseline" / "docling.md",
            )
            self.assertEqual(prepared.pdf_file.read_bytes(), b"pdf")
            self.assertEqual(prepared.docling_json.read_text(encoding="utf-8"), "{}")
            self.assertEqual(
                prepared.docling_markdown.read_text(encoding="utf-8"),
                "# source",
            )

    def test_recovery_stage_preserves_no_sllm_contract(self) -> None:
        command = RestoreDocumentCommand(
            pdf_file=Path("paper.pdf"),
            output_dir=Path("output"),
            document_slug="paper",
        )
        prepared = PreparedRestoration(
            pdf_file=Path("output/paper.pdf"),
            docling_json=Path("output/docling.json"),
            docling_markdown=Path("output/docling.md"),
            manifest_file=Path("output/manifest.json"),
        )

        with mock.patch("subprocess.run") as run:
            SubprocessDocumentRestorationStages().run_stage(
                RestorationStage.RECOVER_BLOCKS,
                command,
                prepared,
            )

        args = run.call_args.args[0]
        self.assertIn(
            "app.modules.document_restoration.infrastructure.recover_blocks_with_ocr_sllm",
            args,
        )
        self.assertIn("--no-sllm", args)
        self.assertIn(str(prepared.manifest_file), args)

    def test_vision_stage_passes_model_prompt_and_attempt_limit(self) -> None:
        command = RestoreDocumentCommand(
            pdf_file=Path("paper.pdf"),
            output_dir=Path("output"),
            document_slug="paper",
            use_local_vision=True,
            vision_model="vision-model",
            max_vision_attempts=2,
        )
        prepared = PreparedRestoration(
            pdf_file=Path("output/paper.pdf"),
            docling_json=Path("output/docling.json"),
            docling_markdown=Path("output/docling.md"),
            manifest_file=Path("output/manifest.json"),
        )

        with mock.patch("subprocess.run") as run:
            SubprocessDocumentRestorationStages().run_stage(
                RestorationStage.REVIEW_BLOCKS_WITH_VISION,
                command,
                prepared,
            )

        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--model") + 1], "vision-model")
        self.assertEqual(args[args.index("--max-attempts") + 1], "2")
        self.assertTrue(Path(args[args.index("--prompt-dir") + 1]).is_dir())

    def test_selective_repair_stage_passes_provider_configuration(self) -> None:
        command = RestoreDocumentCommand(
            pdf_file=Path("paper.pdf"),
            output_dir=Path("output"),
            document_slug="paper",
            selective_provider="gemini",
            selective_model="gemini-3.1-flash-lite",
            selective_max_workers=8,
        )
        prepared = PreparedRestoration(
            pdf_file=Path("output/paper.pdf"),
            docling_json=Path("output/docling.json"),
            docling_markdown=Path("output/docling.md"),
            manifest_file=Path("output/manifest.json"),
        )

        with mock.patch("subprocess.run") as run:
            SubprocessDocumentRestorationStages().run_stage(
                RestorationStage.SELECTIVE_REPAIR_WITH_PROVIDER,
                command,
                prepared,
            )

        args = run.call_args.args[0]
        self.assertIn(
            "app.modules.document_restoration.infrastructure.selective_repair_with_provider",
            args,
        )
        self.assertEqual(args[args.index("--provider") + 1], "gemini")
        self.assertEqual(
            args[args.index("--model") + 1], "gemini-3.1-flash-lite"
        )
        self.assertEqual(args[args.index("--max-workers") + 1], "8")

    def test_detected_markdown_ignores_stale_recovery_results(self) -> None:
        command = RestoreDocumentCommand(
            pdf_file=Path("paper.pdf"),
            output_dir=Path("output"),
            document_slug="paper",
        )
        prepared = PreparedRestoration(
            pdf_file=Path("output/paper.pdf"),
            docling_json=Path("output/docling.json"),
            docling_markdown=Path("output/docling.md"),
            manifest_file=Path("output/manifest.json"),
        )

        with mock.patch("subprocess.run") as run:
            stages = SubprocessDocumentRestorationStages()
            stages.run_stage(
                RestorationStage.ASSEMBLE_DETECTED_MARKDOWN,
                command,
                prepared,
            )
            detected_args = run.call_args.args[0]
            stages.run_stage(
                RestorationStage.ASSEMBLE_MARKDOWN,
                command,
                prepared,
            )
            final_args = run.call_args.args[0]

        self.assertIn("--ignore-recovered-results", detected_args)
        self.assertNotIn("--ignore-recovered-results", final_args)


if __name__ == "__main__":
    unittest.main()
