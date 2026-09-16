from __future__ import annotations

from app.modules.wiki_generation.application.evaluation_guards import (
    repair_notes_from_evaluation,
    repair_normalized_from_evaluation,
)
from app.modules.wiki_generation.application.models import GenerationEvaluation
from app.modules.wiki_generation.application.ports import (
    JsonDict,
)


class EvaluationGuardRepairer:
    def repair(
        self,
        notes: list[JsonDict],
        normalized: JsonDict,
        evaluation: GenerationEvaluation,
    ) -> tuple[list[JsonDict], list[str]]:
        _, operations = repair_normalized_from_evaluation(normalized, evaluation)
        repaired_notes = repair_notes_from_evaluation(notes, evaluation) if operations else notes
        return repaired_notes, operations


def generation_evaluation_finished(
    evaluation: GenerationEvaluation,
    attempt: int,
    max_attempts: int,
) -> bool:
    return bool(
        not evaluation.get("retry_recommended")
        or evaluation.get("passed")
        or attempt >= max_attempts
    )


def generation_retry_prompt(
    semantic_system_prompt: str,
    evaluation: GenerationEvaluation,
) -> str:
    feedback = str(evaluation.get("retry_feedback") or "")
    return (
        semantic_system_prompt
        + "\n\nEvaluator feedback for retry:\n"
        + feedback
        + "\nApply this feedback strictly. Keep source anchors exact. Return the same JSON schema."
    )


def generation_retry_block_ids(
    normalized: JsonDict,
    evaluation: GenerationEvaluation,
    source_block_ids: list[str] | None = None,
    packet_block_ids: dict[str, list[str]] | None = None,
) -> list[str] | None:
    """모든 evaluator target을 source block으로 해석하며, None은 전체 재생성을 뜻합니다."""
    issues = evaluation.get("issues") or []
    if not issues:
        return None

    records = [
        *normalized.get("concept_ledger", []),
        *normalized.get("evidence_units", []),
        *normalized.get("observations", []),
        *normalized.get("section_candidates", []),
        *normalized.get("mentions", []),
        *normalized.get("categories", []),
        *normalized.get("semantic_notes", []),
    ]
    evidence_by_id = {
        str(item.get("evidence_id")): item
        for item in normalized.get("evidence_units", [])
        if item.get("evidence_id")
    }
    known_block_ids = {
        str(block_id)
        for record in [*records, *normalized.get("semantic_notes", [])]
        for block_id in _record_anchor_ids(record)
    }
    valid_source_block_ids = set(source_block_ids) if source_block_ids is not None else None
    resolved: list[str] = []
    for issue in issues:
        raw_targets = issue.get("target") or []
        targets = raw_targets if isinstance(raw_targets, list) else [raw_targets]
        for raw_target in targets:
            target = str(raw_target).strip()
            if not target:
                return None
            target_block_ids: list[str] = []
            direct_source_block = target.startswith("B") and target[1:].isdigit()
            if target in known_block_ids or (
                direct_source_block
                and (valid_source_block_ids is None or target in valid_source_block_ids)
            ):
                target_block_ids.append(target)
            if issue.get("type") == "semantic_coverage_gap" and packet_block_ids:
                target_block_ids.extend(packet_block_ids.get(target, []))
            for record in records:
                if target not in _record_identifiers(record):
                    continue
                target_block_ids.extend(_record_anchor_ids(record))
                for evidence_id in record.get("evidence_claim_ids", []) or []:
                    target_block_ids.extend(_record_anchor_ids(evidence_by_id.get(str(evidence_id), {})))
            target_block_ids = [
                block_id
                for block_id in target_block_ids
                if valid_source_block_ids is None or block_id in valid_source_block_ids
            ]
            if not target_block_ids:
                return None
            resolved.extend(target_block_ids)
    return _unique(resolved)


def generation_evaluation_status(evaluations: list[GenerationEvaluation]) -> str:
    if not evaluations:
        return "disabled"
    final = evaluations[-1]
    return "passed" if final.get("passed") and not final.get("issues") else "unresolved"


def _record_identifiers(record: JsonDict) -> set[str]:
    return {
        str(record.get(field)).strip()
        for field in ("slug", "title", "name", "term", "evidence_id", "observation_id", "chunk_id")
        if record.get(field)
    }


def _record_anchor_ids(record: JsonDict) -> list[str]:
    anchors = list(record.get("anchor_reference_ids") or [])
    for field in (
        "key_points",
        "observations",
        "categories",
        "core_concepts",
        "section_candidates",
        "mentions",
        "concept_candidates",
        "evidence_claims",
    ):
        for item in record.get(field, []) or []:
            anchors.extend(item.get("anchor_reference_ids") or [])
    return _unique([str(anchor) for anchor in anchors if str(anchor)])


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
