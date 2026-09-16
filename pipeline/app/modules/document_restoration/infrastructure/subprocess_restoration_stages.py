from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from app.modules.document_restoration.application.models import (
    PreparedRestoration,
    RestoreDocumentCommand,
    StageTiming,
)
from app.modules.document_restoration.application.ports import (
    DocumentRestorationStagesPort,
)
from app.modules.document_restoration.domain.entities import (
    RestorationMode,
    RestorationStage,
)


MODULE_ROOT = "app.modules.document_restoration.infrastructure"
PROMPT_DIR = Path(__file__).resolve().parents[4] / "prompts" / "document_restoration"


class SubprocessDocumentRestorationStages(DocumentRestorationStagesPort):
    def prepare(self, command: RestoreDocumentCommand) -> PreparedRestoration:
        if (command.docling_json is None) != (command.docling_markdown is None):
            raise ValueError(
                "캐시된 Docling 결과는 --docling-json과 "
                "--docling-markdown을 함께 전달해야 합니다."
            )
        command.output_dir.mkdir(parents=True, exist_ok=True)
        target_pdf = command.output_dir / f"{command.document_slug}.pdf"
        if command.pdf_file.resolve() != target_pdf.resolve():
            shutil.copy2(command.pdf_file, target_pdf)

        baseline_dir = command.output_dir / "layout" / "auto" / "docling_ocr_baseline"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        target_json = baseline_dir / "docling.json"
        target_markdown = baseline_dir / "docling.md"
        if command.docling_json is not None and command.docling_json.resolve() != target_json.resolve():
            shutil.copy2(command.docling_json, target_json)
        if (
            command.docling_markdown is not None
            and command.docling_markdown.resolve() != target_markdown.resolve()
        ):
            shutil.copy2(command.docling_markdown, target_markdown)

        return PreparedRestoration(
            pdf_file=target_pdf,
            docling_json=target_json,
            docling_markdown=target_markdown,
            manifest_file=command.output_dir
            / "layout"
            / "auto"
            / (
                f"{command.document_slug}.crop_first_manifest.json"
                if command.mode is RestorationMode.CROP_FIRST
                else f"{command.document_slug}.docling_primary_manifest.json"
            ),
        )

    def needs_docling_baseline(self, prepared: PreparedRestoration) -> bool:
        return not prepared.docling_json.exists()

    def run_stage(
        self,
        stage: RestorationStage,
        command: RestoreDocumentCommand,
        prepared: PreparedRestoration,
    ) -> StageTiming:
        args = self._stage_command(stage, command, prepared)
        print("+ " + " ".join(args), flush=True)
        started_at = time.perf_counter()
        subprocess.run(args, check=True)
        elapsed_seconds = time.perf_counter() - started_at
        print(f"elapsed_seconds={elapsed_seconds:.2f}", flush=True)
        if stage is RestorationStage.DOCLING_BASELINE:
            self._normalize_docling_outputs(prepared)
        return StageTiming(stage=stage, elapsed_seconds=elapsed_seconds)

    def write_timings(
        self,
        command: RestoreDocumentCommand,
        timings: list[StageTiming],
        total_elapsed_seconds: float,
    ) -> None:
        timing_file = command.output_dir / "final" / f"{command.document_slug}.pipeline_timing.json"
        timing_file.parent.mkdir(parents=True, exist_ok=True)
        timing_file.write_text(
            json.dumps(
                {
                    "document_slug": command.document_slug,
                    "mode": command.mode.value,
                    "total_elapsed_seconds": total_elapsed_seconds,
                    "stages": [
                        {
                            "stage": timing.stage.value,
                            "elapsed_seconds": timing.elapsed_seconds,
                        }
                        for timing in timings
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _stage_command(
        self,
        stage: RestorationStage,
        command: RestoreDocumentCommand,
        prepared: PreparedRestoration,
    ) -> list[str]:
        common = [
            "--output-dir",
            str(command.output_dir),
            "--document-slug",
            command.document_slug,
        ]
        module_args: dict[RestorationStage, tuple[str, list[str]]] = {
            RestorationStage.PREPARE_CROP_FIRST: (
                "crop_first_with_anydoc",
                [
                    "--pdf-file",
                    str(prepared.pdf_file),
                    "--manifest-file",
                    str(prepared.manifest_file),
                    "--detected-markdown",
                    str(command.output_dir / "final" / f"{command.document_slug}.detected.md"),
                    "--output-dir",
                    str(command.output_dir),
                    "--anydoc-command",
                    command.anydoc_command,
                    "--heron-command",
                    command.heron_command,
                    "--body-ai-budget",
                    str(command.body_ai_budget),
                    *(["--heron-model", str(command.heron_model)] if command.heron_model else []),
                    *(["--pdfium-library", str(command.pdfium_library)] if command.pdfium_library else []),
                ],
            ),
            RestorationStage.ASSEMBLE_CROP_FIRST: (
                "crop_first_with_anydoc",
                [
                    "--pdf-file",
                    str(prepared.pdf_file),
                    "--manifest-file",
                    str(prepared.manifest_file),
                    "--detected-markdown",
                    str(command.output_dir / "final" / f"{command.document_slug}.detected.md"),
                    "--output-dir",
                    str(command.output_dir),
                    "--output-file",
                    str(command.output_dir / "final" / f"{command.document_slug}.restored.md"),
                    "--assemble-only",
                ],
            ),
            RestorationStage.PUBLISH_DOCLING_MARKDOWN: (
                "publish_docling_markdown",
                [
                    "--input-file",
                    str(prepared.docling_markdown),
                    "--output-file",
                    str(
                        command.output_dir
                        / "final"
                        / f"{command.document_slug}.restored.md"
                    ),
                ],
            ),
            RestorationStage.DETECT_LAYOUT_BLOCKS: (
                "detect_layout_blocks",
                ["--pdf-file", str(prepared.pdf_file), *common],
            ),
            RestorationStage.DETECT_EQUATION_CANDIDATES: (
                "detect_docling_equation_candidates",
                ["--pdf-file", str(prepared.pdf_file), *common],
            ),
            RestorationStage.BUILD_PRIMARY_MANIFEST: (
                "build_docling_primary_manifest",
                common,
            ),
            RestorationStage.AUGMENT_TEXT_CANDIDATES: (
                "augment_text_candidates_with_crop_ocr",
                [*common, "--manifest-file", str(prepared.manifest_file)],
            ),
            RestorationStage.ASSEMBLE_DETECTED_MARKDOWN: (
                "process_auto_layout_blocks",
                self._assembly_args(
                    command,
                    prepared,
                    command.output_dir
                    / "final"
                    / f"{command.document_slug}.detected.md",
                    ignore_recovered_results=True,
                ),
            ),
            RestorationStage.SELECTIVE_REPAIR_WITH_PROVIDER: (
                "selective_repair_with_provider",
                [
                    "--pdf-file",
                    str(prepared.pdf_file),
                    "--manifest-file",
                    str(prepared.manifest_file),
                    "--detected-markdown",
                    str(
                        command.output_dir
                        / "final"
                        / f"{command.document_slug}.detected.md"
                    ),
                    "--output-dir",
                    str(command.output_dir),
                    "--provider",
                    command.selective_provider,
                    "--model",
                    command.selective_model,
                    "--max-workers",
                    str(command.selective_max_workers),
                ],
            ),
            RestorationStage.RECOVER_BLOCKS: (
                "recover_blocks_with_ocr_sllm",
                self._recovery_args(command, prepared, common),
            ),
            RestorationStage.REVIEW_BLOCKS_WITH_VISION: (
                "review_blocks_with_vision",
                self._vision_args(command, prepared),
            ),
            RestorationStage.RECOVER_FIGURES_WITH_VISION: (
                "recover_figure_blocks_with_vision",
                self._vision_args(command, prepared),
            ),
            RestorationStage.ASSEMBLE_MARKDOWN: (
                "process_auto_layout_blocks",
                self._assembly_args(
                    command,
                    prepared,
                    command.output_dir
                    / "final"
                    / f"{command.document_slug}.restored.md",
                ),
            ),
        }
        if stage is RestorationStage.DOCLING_BASELINE:
            return [
                command.docling_command,
                "convert",
                str(prepared.pdf_file),
                "--to",
                "json",
                "--to",
                "md",
                "--pipeline",
                "standard",
                "--ocr",
                "--tables",
                "--table-mode",
                "accurate",
                "--image-export-mode",
                "placeholder",
                "--output",
                str(prepared.docling_json.parent),
            ]
        module, args = module_args[stage]
        return [sys.executable, "-m", f"{MODULE_ROOT}.{module}", *args]

    def _assembly_args(
        self,
        command: RestoreDocumentCommand,
        prepared: PreparedRestoration,
        output_file: Path,
        *,
        ignore_recovered_results: bool = False,
    ) -> list[str]:
        args = [
            "--output-dir",
            str(command.output_dir),
            "--document-slug",
            command.document_slug,
            "--manifest-file",
            str(prepared.manifest_file),
            "--output-file",
            str(output_file),
            "--report-file",
            str(
                command.output_dir
                / "final"
                / f"{command.document_slug}.restoration_report.md"
            ),
            "--source-name",
            prepared.pdf_file.name,
        ]
        if ignore_recovered_results:
            args.append("--ignore-recovered-results")
        return args

    def _recovery_args(
        self,
        command: RestoreDocumentCommand,
        prepared: PreparedRestoration,
        common: list[str],
    ) -> list[str]:
        args = [*common, "--manifest-file", str(prepared.manifest_file)]
        if command.use_local_sllm:
            args.extend(["--endpoint", command.endpoint, "--model", command.model])
        else:
            args.append("--no-sllm")
        return args

    def _vision_args(
        self,
        command: RestoreDocumentCommand,
        prepared: PreparedRestoration,
    ) -> list[str]:
        return [
            "--base-dir",
            str(command.output_dir),
            "--document-slug",
            command.document_slug,
            "--manifest-file",
            str(prepared.manifest_file),
            "--prompt-dir",
            str(PROMPT_DIR),
            "--endpoint",
            command.endpoint,
            "--model",
            command.vision_model,
            "--max-attempts",
            str(command.max_vision_attempts),
        ]

    def _normalize_docling_outputs(self, prepared: PreparedRestoration) -> None:
        baseline_dir = prepared.docling_json.parent
        generated_json = baseline_dir / f"{prepared.pdf_file.stem}.json"
        generated_markdown = baseline_dir / f"{prepared.pdf_file.stem}.md"
        if not generated_json.exists():
            raise FileNotFoundError(f"Docling JSON output not found: {generated_json}")
        generated_json.replace(prepared.docling_json)
        if generated_markdown.exists():
            generated_markdown.replace(baseline_dir / "docling.md")
