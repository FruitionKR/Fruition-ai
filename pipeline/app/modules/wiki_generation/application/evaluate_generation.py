from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import asdict
from typing import Any

from app.modules.wiki_generation.application.evaluation_guards import (
    apply_generation_evaluation_guards,
)
from app.modules.wiki_generation.application.models import GenerationEvaluation
from app.modules.wiki_generation.application.ports import JsonCompletionPort

# 문서 전체를 한 요청에 넣으면 큰 문서가 모델 입력 한도를 넘는다(1MB 문서의 평가 입력 약 290만 자).
# 원문 블록을 이 글자 수 단위 구간으로 나눠 평가하고 결과를 합친다.
EVALUATION_WINDOW_CHARS = 120_000
EVALUATION_MAX_WORKERS = 4
NORMALIZED_LIST_KEYS = (
    "semantic_notes",
    "concept_ledger",
    "categories",
    "section_candidates",
    "mentions",
    "observations",
    "evidence_units",
)


def evaluate_generation(
    *,
    completion: JsonCompletionPort,
    evaluator_prompt: str,
    document: Any,
    blocks: list[Any],
    normalized: dict[str, Any],
) -> GenerationEvaluation:
    windows = _block_windows(blocks, EVALUATION_WINDOW_CHARS)
    if len(windows) == 1:
        evaluation = _evaluate_window(completion, evaluator_prompt, document, blocks, normalized)
    else:
        subsets = _normalized_by_window(normalized, windows)
        with ThreadPoolExecutor(max_workers=min(EVALUATION_MAX_WORKERS, len(windows))) as executor:
            futures = [
                executor.submit(
                    copy_context().run,
                    _evaluate_window,
                    completion,
                    evaluator_prompt,
                    document,
                    window,
                    subset,
                )
                for window, subset in zip(windows, subsets, strict=True)
            ]
            results = [future.result() for future in futures]
        evaluation = _merge_evaluations(results, [len(window) for window in windows])
    apply_generation_evaluation_guards(
        evaluation,
        normalized,
        source_block_ids=[block.block_id for block in blocks],
    )
    return evaluation


def _evaluate_window(
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
            **{key: normalized.get(key, []) for key in NORMALIZED_LIST_KEYS},
            "warnings": normalized.get("warnings", []),
        },
    }
    evaluation: GenerationEvaluation = completion.complete_json(
        evaluator_prompt,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    _normalize_evaluation(evaluation)
    return evaluation


def _block_windows(blocks: list[Any], max_chars: int) -> list[list[Any]]:
    windows: list[list[Any]] = [[]]
    chars = 0
    for block in blocks:
        if windows[-1] and chars + len(block.text) > max_chars:
            windows.append([])
            chars = 0
        windows[-1].append(block)
        chars += len(block.text)
    return windows


def _normalized_by_window(
    normalized: dict[str, Any],
    windows: list[list[Any]],
) -> list[dict[str, Any]]:
    """구간마다 그 구간 블록을 근거로 가리키는 항목만 고른다. 근거가 없는 항목은 첫 구간에 둔다."""
    window_ids = [
        {block.block_id for block in window} | {block.source_reference_id for block in window}
        for window in windows
    ]
    subsets: list[dict[str, Any]] = [
        {key: [] for key in NORMALIZED_LIST_KEYS} for _ in windows
    ]
    for key in NORMALIZED_LIST_KEYS:
        for item in normalized.get(key, []):
            anchors = _anchor_ids(item)
            targets = [index for index, ids in enumerate(window_ids) if anchors & ids] or [0]
            for index in targets:
                subsets[index][key].append(item)
    for subset in subsets:
        subset["warnings"] = normalized.get("warnings", [])
    return subsets


def _anchor_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        ids: set[str] = set()
        for key, child in value.items():
            if key in {"anchor_reference_ids", "anchor_block_ids"} and isinstance(child, list):
                ids.update(str(item) for item in child)
            else:
                ids |= _anchor_ids(child)
        return ids
    if isinstance(value, list):
        return set().union(*(_anchor_ids(item) for item in value)) if value else set()
    return set()


def _merge_evaluations(
    results: list[GenerationEvaluation],
    weights: list[int],
) -> GenerationEvaluation:
    scores: dict[str, float] = {}
    for metric in sorted({metric for result in results for metric in result["scores"]}):
        scored = [
            (result["scores"][metric], weight)
            for result, weight in zip(results, weights, strict=True)
            if metric in result["scores"]
        ]
        scores[metric] = sum(score * weight for score, weight in scored) / sum(weight for _, weight in scored)
    return {
        "scores": scores,
        "passed": all(result["passed"] for result in results),
        "retry_recommended": any(result["retry_recommended"] for result in results),
        "issues": [issue for result in results for issue in result["issues"]],
        "warnings": [warning for result in results for warning in result["warnings"]],
        "retry_feedback": "\n".join(
            result["retry_feedback"] for result in results if result["retry_feedback"].strip()
        ),
    }


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
