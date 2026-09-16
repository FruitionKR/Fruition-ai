from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LocalDocumentEvaluationCommand:
    markdown_file: Path
    pdf_file: Path
    output_file: Path
    output_markdown_file: Path | None
    output_report_file: Path | None
    endpoint: str
    evaluator_model: str
    vision_model: str
    max_blocks: int
    max_chars: int
    max_vision_attempts: int
    max_vision_requests: int
    max_chunks: int
    resume: bool
    dry_run: bool
