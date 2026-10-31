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
# 노트 안의 추출 항목은 정규화 때 최상위 목록으로 펼쳐지므로, 평가 입력에서는 노트에서 빼고 한 번만 보낸다.
NOTE_ITEM_KEYS = (
    "observations",
    "categories",
    "core_concepts",
    "concept_candidates",
    "section_candidates",
    "mentions",
    "evidence_claims",
)
# 평가 판단에 쓰지 않는 필드: 단일 문서라 항목마다 같은 문서 ID, 집계값, 다른 쪽 목록에 이미 있는 역방향 ID.
EVALUATION_OMITTED_ITEM_KEYS = {
    "source_document_id",
    "source_document_ids",
    "mention_count",
    "importance_score",
    "display_reference_ids",
    "mention_reference_ids",
    "evidence_claim_ids",
}
# 평가자 프롬프트의 합격 점수. 높음 심각도 문제가 없고 이 점수를 넘으면 합격이다.
PASS_SCORE_THRESHOLDS = {
    "source_excerpt_fidelity": 4,
    "concept_groundedness": 4,
    "relation_faithfulness": 0.75,
    "evidence_relevance": 0.75,
}


def evaluate_generation(
    *,
    completion: JsonCompletionPort,
    evaluator_prompt: str,
    document: Any,
    blocks: list[Any],
    normalized: dict[str, Any],
    previous_requests: list[dict[str, Any]],
) -> GenerationEvaluation:
    windows = _block_windows(blocks, EVALUATION_WINDOW_CHARS)
    if len(windows) == 1:
        evaluation = _evaluate_window(completion, evaluator_prompt, document, blocks, normalized, previous_requests)
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
                    previous_requests,
                )
                for window, subset in zip(windows, subsets, strict=True)
            ]
            results = [future.result() for future in futures]
        evaluation = _merge_evaluations(results, [len(window) for window in windows])
    apply_generation_evaluation_guards(
        evaluation,
        normalized,
        source_block_ids=[block.block_id for block in blocks],
        # ponytail: 재편입은 바뀐 블록만 받아 그대로인 블록의 숫자도 문제로 본다. 편입 뒤 제거는 전체 블록 기준이다.
        source_texts=[block.text for block in blocks],
    )
    return evaluation


def _evaluate_window(
    completion: JsonCompletionPort,
    evaluator_prompt: str,
    document: Any,
    blocks: list[Any],
    normalized: dict[str, Any],
    previous_requests: list[dict[str, Any]],
) -> GenerationEvaluation:
    payload = {
        "document": asdict(document),
        "source_blocks": [
            {"block_id": block.block_id, "text": block.text}
            for block in blocks
        ],
        "normalized": {
            **{
                key: [_evaluation_item(item) for item in normalized.get(key, [])]
                for key in NORMALIZED_LIST_KEYS
                if key != "semantic_notes"
            },
            "semantic_notes": [
                {key: value for key, value in note.items() if key not in NOTE_ITEM_KEYS}
                for note in normalized.get("semantic_notes", [])
            ],
            "warnings": normalized.get("warnings", []),
        },
        "previous_requests": previous_requests,
    }
    evaluation: GenerationEvaluation = completion.complete_json(
        evaluator_prompt,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    _normalize_evaluation(evaluation)
    _apply_blocking_rule(evaluation)
    return evaluation


def _apply_blocking_rule(evaluation: GenerationEvaluation) -> None:
    """중간·낮음 심각도 문제는 경고로 옮겨, 그것만으로는 불합격·재시도하지 않게 한다. 심각도가 없으면 막는 문제로 본다."""
    blocking = [issue for issue in evaluation["issues"] if issue.get("severity") not in {"medium", "low"}]
    if len(blocking) == len(evaluation["issues"]):
        return
    evaluation["warnings"] = [*evaluation["warnings"], *(issue for issue in evaluation["issues"] if issue not in blocking)]
    evaluation["issues"] = blocking
    # 평가자가 옮긴 문제 때문에 불합격시켰을 수 있어, 남은 문제가 없으면 점수 기준으로 다시 판정한다.
    evaluation["passed"] = not blocking and all(
        isinstance(evaluation["scores"].get(metric), int | float) and evaluation["scores"][metric] >= threshold
        for metric, threshold in PASS_SCORE_THRESHOLDS.items()
    )
    evaluation["retry_recommended"] = not evaluation["passed"]
    evaluation["retry_feedback"] = " ".join(
        dict.fromkeys(str(issue["feedback"]) for issue in blocking if issue.get("feedback"))
    )


def _evaluation_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    omitted = set(EVALUATION_OMITTED_ITEM_KEYS)
    # term은 정규화가 name/title을 복사해 둔 값이라, 같으면 한 번만 보낸다.
    if item.get("term") is not None and item.get("term") in (item.get("name"), item.get("title")):
        omitted.add("term")
    return {key: value for key, value in item.items() if key not in omitted}


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
