import unittest
from pathlib import Path
from unittest.mock import patch

from app.modules.document_restoration.application.models import (
    PreparedRestoration,
    RestoreDocumentCommand,
    StageTiming,
)
from app.modules.document_restoration.application.restore_document import (
    RestoreDocumentUseCase,
)
from app.modules.document_restoration.domain.entities import (
    RestorationMode,
    RestorationStage,
)
from app.modules.document_restoration.interfaces.cli import main


class FakeStages:
    def __init__(self, needs_docling_baseline: bool) -> None:
        self._needs_docling_baseline = needs_docling_baseline
        self.stages: list[RestorationStage] = []
        self.written_timings: list[StageTiming] = []

    def prepare(self, command: RestoreDocumentCommand) -> PreparedRestoration:
        return PreparedRestoration(
            pdf_file=command.pdf_file,
            docling_json=Path("docling.json"),
            docling_markdown=Path("docling.md"),
            manifest_file=Path("manifest.json"),
        )

    def needs_docling_baseline(self, prepared: PreparedRestoration) -> bool:
        return self._needs_docling_baseline

    def run_stage(
        self,
        stage: RestorationStage,
        command: RestoreDocumentCommand,
        prepared: PreparedRestoration,
    ) -> StageTiming:
        self.stages.append(stage)
        return StageTiming(stage=stage, elapsed_seconds=1.0)

    def write_timings(
        self,
        command: RestoreDocumentCommand,
        timings: list[StageTiming],
        total_elapsed_seconds: float,
    ) -> None:
        self.written_timings = timings


class RestoreDocumentUseCaseTest(unittest.TestCase):
    def test_runs_crop_first_by_default(self) -> None:
        stages = FakeStages(needs_docling_baseline=False)

        RestoreDocumentUseCase(stages).execute(
            RestoreDocumentCommand(
                pdf_file=Path("paper.pdf"),
                output_dir=Path("output"),
                document_slug="paper",
            )
        )

        self.assertEqual(
            stages.stages,
            [
                RestorationStage.PREPARE_CROP_FIRST,
                RestorationStage.SELECTIVE_REPAIR_WITH_PROVIDER,
                RestorationStage.ASSEMBLE_CROP_FIRST,
            ],
        )
        self.assertEqual(len(stages.written_timings), 3)

    def test_runs_full_repair_stages_when_requested(self) -> None:
        stages = FakeStages(needs_docling_baseline=False)

        RestoreDocumentUseCase(stages).execute(
            RestoreDocumentCommand(
                pdf_file=Path("paper.pdf"),
                output_dir=Path("output"),
                document_slug="paper",
                mode=RestorationMode.FULL_REPAIR,
            )
        )

        self.assertEqual(
            stages.stages,
            [
                RestorationStage.DETECT_LAYOUT_BLOCKS,
                RestorationStage.DETECT_EQUATION_CANDIDATES,
                RestorationStage.BUILD_PRIMARY_MANIFEST,
                RestorationStage.AUGMENT_TEXT_CANDIDATES,
                RestorationStage.RECOVER_BLOCKS,
                RestorationStage.ASSEMBLE_MARKDOWN,
            ],
        )

    def test_runs_selective_repair_without_legacy_recovery_stages(self) -> None:
        stages = FakeStages(needs_docling_baseline=False)

        RestoreDocumentUseCase(stages).execute(
            RestoreDocumentCommand(
                pdf_file=Path("paper.pdf"),
                output_dir=Path("output"),
                document_slug="paper",
                mode=RestorationMode.SELECTIVE_REPAIR,
            )
        )

        self.assertEqual(
            stages.stages,
            [
                RestorationStage.DETECT_LAYOUT_BLOCKS,
                RestorationStage.DETECT_EQUATION_CANDIDATES,
                RestorationStage.BUILD_PRIMARY_MANIFEST,
                RestorationStage.AUGMENT_TEXT_CANDIDATES,
                RestorationStage.ASSEMBLE_DETECTED_MARKDOWN,
                RestorationStage.SELECTIVE_REPAIR_WITH_PROVIDER,
                RestorationStage.ASSEMBLE_MARKDOWN,
            ],
        )
        self.assertNotIn(RestorationStage.RECOVER_BLOCKS, stages.stages)
        self.assertNotIn(
            RestorationStage.REVIEW_BLOCKS_WITH_VISION,
            stages.stages,
        )

    def test_adds_docling_and_vision_stages_when_requested(self) -> None:
        stages = FakeStages(needs_docling_baseline=True)

        RestoreDocumentUseCase(stages).execute(
            RestoreDocumentCommand(
                pdf_file=Path("paper.pdf"),
                output_dir=Path("output"),
                document_slug="paper",
                mode=RestorationMode.FULL_REPAIR,
                use_local_vision=True,
            )
        )

        self.assertEqual(stages.stages[0], RestorationStage.DOCLING_BASELINE)
        self.assertIn(RestorationStage.REVIEW_BLOCKS_WITH_VISION, stages.stages)
        self.assertIn(RestorationStage.RECOVER_FIGURES_WITH_VISION, stages.stages)
        self.assertEqual(stages.stages[-1], RestorationStage.ASSEMBLE_MARKDOWN)

    def test_cli_uses_provider_default_model_when_model_is_omitted(self) -> None:
        for provider, model in (
            ("openai", "gpt-5-nano"),
            ("claude", "claude-sonnet-5"),
        ):
            with self.subTest(provider=provider):
                argv = [
                    "restore-document",
                    "--pdf-file", "paper.pdf",
                    "--output-dir", "output",
                    "--document-slug", "paper",
                    "--selective-provider", provider,
                ]
                with patch("sys.argv", argv), patch.object(
                    RestoreDocumentUseCase, "execute"
                ) as execute:
                    main()

                command = execute.call_args.args[0]
                self.assertEqual(command.selective_provider, provider)
                self.assertEqual(command.selective_model, model)


if __name__ == "__main__":
    unittest.main()
