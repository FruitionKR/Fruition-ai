from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from app.modules.wiki_generation.application.evaluation_guards import (
    apply_generation_evaluation_guards,
)
from app.modules.wiki_generation.application.models import GenerationEvaluation
from app.modules.wiki_generation.application.ports import JsonCompletionPort


def evaluate_generation(
    *,
    completion: JsonCompletionPort,
    evaluator_prompt: str,
    document: Any,
    blocks: list[Any],
    normalized: dict[str, Any],
) -> GenerationEvaluation:
    payload = {
        "document": asdict(document),
        "source_blocks": [
            {"block_id": block.block_id, "text": block.text}
            for block in blocks
        ],
        "normalized": {
            "semantic_notes": normalized.get("semantic_notes", []),
            "concept_ledger": normalized.get("concept_ledger", []),
            "categories": normalized.get("categories", []),
            "section_candidates": normalized.get("section_candidates", []),
            "mentions": normalized.get("mentions", []),
            "observations": normalized.get("observations", []),
            "evidence_units": normalized.get("evidence_units", []),
            "warnings": normalized.get("warnings", []),
        },
    }
    evaluation: GenerationEvaluation = completion.complete_json(
        evaluator_prompt,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    _normalize_evaluation(evaluation)
    apply_generation_evaluation_guards(
        evaluation,
        normalized,
        source_block_ids=[block.block_id for block in blocks],
    )
    return evaluation


def _normalize_evaluation(evaluation: GenerationEvaluation) -> None:
    invalid_fields: list[str] = []
    defaults = {
        "scores": {},
        "passed": False,
        "issues": [],
        "warnings": [],
        "retry_feedback": "",
    }
    for field, default in defaults.items():
        value = evaluation.setdefault(field, default)
        if isinstance(value, type(default)):
            continue
        evaluation[field] = default
        invalid_fields.append(field)

    scores = evaluation["scores"]
    numeric_scores = {
        str(metric): score
        for metric, score in scores.items()
        if isinstance(score, int | float) and not isinstance(score, bool)
    }
    if len(numeric_scores) != len(scores):
        evaluation["scores"] = numeric_scores
        invalid_fields.append("scores")

    retry_recommended = evaluation.setdefault(
        "retry_recommended",
        not evaluation["passed"],
    )
    if not isinstance(retry_recommended, bool):
        evaluation["retry_recommended"] = not evaluation["passed"]
        invalid_fields.append("retry_recommended")

    for field in ("issues", "warnings"):
        items = evaluation[field]
        valid_items = [item for item in items if isinstance(item, dict)]
        if len(valid_items) != len(items):
            evaluation[field] = valid_items
            invalid_fields.append(field)

    if invalid_fields:
        evaluation["issues"].append(
            {
                "metric": "evaluator_contract",
                "type": "invalid_evaluator_response",
                "severity": "high",
                "target": [],
                "reason": f"evaluator 응답 필드 형식이 올바르지 않음: {', '.join(invalid_fields)}",
                "feedback": "evaluator 응답 형식을 확인한 뒤 semantic extraction을 다시 평가하세요.",
            }
        )
