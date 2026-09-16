from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.modules.document_restoration.domain.entities import (
    RestorationMode,
    RestorationStage,
)


@dataclass(frozen=True)
class RestoreDocumentCommand:
    pdf_file: Path
    output_dir: Path
    document_slug: str
    docling_json: Path | None = None
    docling_markdown: Path | None = None
    mode: RestorationMode = RestorationMode.CROP_FIRST
    use_local_sllm: bool = False
    use_local_vision: bool = False
    endpoint: str = "http://127.0.0.1:11434/v1/chat/completions"
    model: str = "qwen2.5:7b"
    vision_model: str = "qwen2.5vl:7b"
    max_vision_attempts: int = 3
    docling_command: str = "docling"
    selective_provider: str = "gemini"
    selective_model: str = "gemini-3.1-flash-lite"
    selective_max_workers: int = 16
    anydoc_command: str = "anydoc"
    heron_command: str = "raw-special-regions"
    heron_model: Path | None = None
    pdfium_library: Path | None = None
    body_ai_budget: float = 0.3


@dataclass(frozen=True)
class PreparedRestoration:
    pdf_file: Path
    docling_json: Path
    docling_markdown: Path
    manifest_file: Path


@dataclass(frozen=True)
class StageTiming:
    stage: RestorationStage
    elapsed_seconds: float
