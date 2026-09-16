from __future__ import annotations

from typing import Any, TypedDict


class GenerationEvaluation(TypedDict, total=False):
    scores: dict[str, int | float]
    passed: bool
    retry_recommended: bool
    issues: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    retry_feedback: str
    retry_mode: str
    repair_operations: list[str]
    applied_patch_operations: list[dict[str, Any]]
