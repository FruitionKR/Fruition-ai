from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from app.modules.wiki_generation.application.models import GenerationEvaluation


def apply_generation_evaluation_guards(
    evaluation: GenerationEvaluation,
    normalized: dict[str, Any],
    source_block_ids: list[str] | None = None,
) -> None:
    if not evaluation.get("passed"):
        evaluation["retry_recommended"] = True
    _apply_reference_guards(evaluation, normalized, source_block_ids)
    _apply_packet_completeness_guards(evaluation, normalized, source_block_ids)
    core_slugs = {str(item.get("slug")) for item in normalized.get("concept_ledger", [])}
    metadata_fragments = {
        "citation-marker",
        "citation-rank",
        "retrieval-rank",
        "source-block",
        "web-url",
        "confidence",
    }
    fragmented = sorted(core_slugs.intersection(metadata_fragments))
    if len(fragmented) >= 3:
        _append_eval_issue(
            evaluation,
            {
                "metric": "concept_groundedness",
                "type": "over_fragmented_concept",
                "severity": "medium",
                "target": fragmented,
                "reason": "citation/source metadata가 독립 core concept로 과하게 분리됨",
                "feedback": "citation marker, citation_rank, retrieval_rank, source block, web URL, confidence는 독립 core concept가 아니라 citation/provenance metadata의 section_candidate 또는 mention으로 낮추세요.",
            },
        )
    _apply_observation_evaluation_guards(evaluation, normalized)
    if evaluation.get("issues"):
        evaluation["passed"] = False
        evaluation["retry_recommended"] = True
        scores = evaluation.setdefault("scores", {})
        if isinstance(scores.get("overall"), int | float):
            scores["overall"] = min(float(scores["overall"]), 0.74)
        feedbacks = [str(issue.get("feedback")) for issue in evaluation.get("issues", []) if issue.get("feedback")]
        evaluation["retry_feedback"] = " ".join(_unique(feedbacks))


def _apply_reference_guards(
    evaluation: GenerationEvaluation,
    normalized: dict[str, Any],
    source_block_ids: list[str] | None,
) -> None:
    allowed_refs = set(source_block_ids) if source_block_ids is not None else None
    collections = (
        ("semantic_notes", normalized.get("semantic_notes", [])),
        ("concept_ledger", normalized.get("concept_ledger", [])),
        ("observations", normalized.get("observations", [])),
        ("section_candidates", normalized.get("section_candidates", [])),
        ("mentions", normalized.get("mentions", [])),
        ("evidence_units", normalized.get("evidence_units", [])),
    )
    for collection, items in collections:
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            _check_reference_item(
                evaluation,
                item,
                collection,
                index,
                allowed_refs,
            )
            if collection != "semantic_notes":
                continue
            for nested_collection in (
                "key_points",
                "observations",
                "core_concepts",
                "section_candidates",
                "mentions",
                "evidence_claims",
            ):
                nested_items = item.get(nested_collection, [])
                if not isinstance(nested_items, list):
                    continue
                for nested_index, nested_item in enumerate(nested_items):
                    if isinstance(nested_item, dict):
                        _check_reference_item(
                            evaluation,
                            nested_item,
                            nested_collection,
                            nested_index,
                            allowed_refs,
                            chunk_id=str(item.get("chunk_id") or ""),
                        )


def _check_reference_item(
    evaluation: GenerationEvaluation,
    item: dict[str, Any],
    collection: str,
    index: int,
    allowed_refs: set[str] | None,
    *,
    chunk_id: str = "",
) -> None:
    if collection == "categories" or not _has_factual_content(item):
        return
    refs = _item_refs(item)
    target = _item_target(item, chunk_id or f"{collection}[{index}]")
    if not refs:
        if collection == "semantic_notes":
            return
        _append_eval_issue(
            evaluation,
            {
                "metric": "source_faithfulness",
                "type": "missing_ref",
                "severity": "high",
                "target": [target],
                "reason": "사실성 있는 의미 항목에 직접 source block ref가 없습니다.",
                "feedback": "모든 factual key point, observation, concept, section, mention, evidence claim에 실제 source block ref를 포함하세요.",
            },
        )
        return
    if allowed_refs is not None:
        invalid_refs = sorted(set(refs) - allowed_refs)
        if invalid_refs:
            _append_eval_issue(
                evaluation,
                {
                    "metric": "source_faithfulness",
                    "type": "invalid_ref",
                    "severity": "high",
                    "target": [target],
                    "reason": f"source block allow-list에 없는 ref가 포함되었습니다: {', '.join(invalid_refs)}",
                    "feedback": "source_blocks에 제공된 block_id만 사용하고 존재하지 않는 ref는 제거하세요.",
                },
            )


def _apply_packet_completeness_guards(
    evaluation: GenerationEvaluation,
    normalized: dict[str, Any],
    source_block_ids: list[str] | None,
) -> None:
    notes = normalized.get("semantic_notes", [])
    if source_block_ids is None or not isinstance(notes, list):
        return
    for note in notes:
        if not isinstance(note, dict):
            continue
        if not _has_meaningful_semantic_content(note):
            continue
        if _record_anchor_ids(note):
            continue
        chunk_id = str(note.get("chunk_id") or "unknown")
        _append_eval_issue(
            evaluation,
            {
                "metric": "source_coverage",
                "type": "semantic_coverage_gap",
                "severity": "high",
                "target": [chunk_id],
                "reason": "packet의 의미 노트에 직접 source anchor가 있는 factual item이 없습니다.",
                "feedback": "모든 packet을 전체 문서 맥락에서 읽고, 의미를 담은 key point·observation·concept·evidence를 직접 anchor와 함께 남기세요.",
            },
        )


def _has_meaningful_semantic_content(note: dict[str, Any]) -> bool:
    if any(
        str(note.get(field) or "").strip()
        for field in ("semantic_summary", "summary", "text", "claim", "definition")
    ):
        return True
    for field in (
        "key_points",
        "observations",
        "core_concepts",
        "section_candidates",
        "mentions",
        "evidence_claims",
    ):
        if any(
            isinstance(item, dict) and _has_factual_content(item)
            for item in note.get(field, []) or []
        ):
            return True
    return False


def _has_factual_content(item: dict[str, Any]) -> bool:
    return any(
        str(item.get(field) or "").strip()
        for field in (
            "text",
            "title",
            "name",
            "term",
            "slug",
            "claim",
            "summary",
            "definition",
            "semantic_summary",
            "context",
        )
    )


def _item_refs(item: dict[str, Any]) -> list[str]:
    return _unique(
        [
            str(ref)
            for field in ("anchor_reference_ids", "anchor_block_ids", "evidence_block_ids")
            for ref in item.get(field, []) or []
            if str(ref).strip()
        ]
    )


def _item_target(item: dict[str, Any], fallback: str) -> str:
    for field in ("slug", "observation_id", "evidence_id", "title", "name", "term", "text", "claim"):
        value = str(item.get(field) or "").strip()
        if value:
            return value
    return fallback


def _record_anchor_ids(record: dict[str, Any]) -> list[str]:
    anchors = _item_refs(record)
    for field in (
        "key_points",
        "observations",
        "core_concepts",
        "section_candidates",
        "mentions",
        "evidence_claims",
    ):
        for item in record.get(field, []) or []:
            if isinstance(item, dict):
                anchors.extend(_item_refs(item))
    return _unique(anchors)


def repair_normalized_from_evaluation(
    normalized: dict[str, Any],
    evaluation: GenerationEvaluation,
) -> tuple[dict[str, Any], list[str]]:
    repairable_types = {"observation_missing_ref", "broken_observation", "duplicate_observation"}
    issues = [issue for issue in evaluation.get("issues", []) if issue.get("type") in repairable_types]
    if not issues:
        return normalized, []
    repaired = {**normalized}
    observations = [dict(item) for item in normalized.get("observations", [])]
    if not observations:
        return normalized, []

    remove_ids: set[str] = set()
    duplicate_groups: list[list[str]] = []
    for issue in issues:
        targets = [str(target) for target in issue.get("target", []) if str(target)]
        if issue.get("type") in {"observation_missing_ref", "broken_observation"}:
            remove_ids.update(targets)
        elif issue.get("type") == "duplicate_observation" and len(targets) > 1:
            duplicate_groups.append(targets)

    operations: list[str] = []
    observations_by_id = {str(item.get("observation_id")): item for item in observations}
    for ids in duplicate_groups:
        candidates = [observations_by_id[item_id] for item_id in ids if item_id in observations_by_id and item_id not in remove_ids]
        if len(candidates) < 2:
            continue
        keeper = _select_observation_keeper(candidates)
        for candidate in candidates:
            if candidate is keeper:
                continue
            _merge_observation(keeper, candidate)
            remove_ids.add(str(candidate.get("observation_id")))
        operations.append(f"merged duplicate observations {ids} into {keeper.get('observation_id')}")

    before_count = len(observations)
    observations = [item for item in observations if str(item.get("observation_id")) not in remove_ids]
    if len(observations) != before_count:
        operations.append(f"removed {before_count - len(observations)} broken or duplicate observations")
    observations = _renumber_observations(observations)
    repaired["observations"] = observations

    repaired_notes = []
    valid_signatures = {_observation_content_signature(item) for item in observations}
    for note in normalized.get("semantic_notes", []):
        note_copy = {**note}
        note_observations = []
        for observation in note.get("observations", []):
            if _observation_content_signature(observation) in valid_signatures:
                note_observations.append(observation)
        note_copy["observations"] = note_observations
        repaired_notes.append(note_copy)
    repaired["semantic_notes"] = repaired_notes
    return repaired, operations


def repair_notes_from_evaluation(
    notes: list[dict[str, Any]],
    evaluation: GenerationEvaluation,
) -> list[dict[str, Any]]:
    repairable_types = {"observation_missing_ref", "broken_observation", "duplicate_observation"}
    issues = [issue for issue in evaluation.get("issues", []) if issue.get("type") in repairable_types]
    if not issues:
        return notes

    repaired_notes = deepcopy(notes)
    observations_by_id: dict[str, dict[str, Any]] = {}
    observation_ids_by_location: dict[tuple[int, int], str] = {}
    for note_index, note in enumerate(repaired_notes):
        for item_index, observation in enumerate(note.get("observations", []) or []):
            if not isinstance(observation, dict):
                continue
            if not str(observation.get("title") or "").strip() and not str(observation.get("summary") or "").strip():
                continue
            observation_id = f"O{len(observations_by_id) + 1:03d}"
            observations_by_id[observation_id] = observation
            observation_ids_by_location[(note_index, item_index)] = observation_id

    remove_ids: set[str] = set()
    duplicate_groups: list[list[str]] = []
    for issue in issues:
        targets = [str(target) for target in issue.get("target", []) if str(target)]
        if issue.get("type") in {"observation_missing_ref", "broken_observation"}:
            remove_ids.update(targets)
        elif issue.get("type") == "duplicate_observation" and len(targets) > 1:
            duplicate_groups.append(targets)

    for ids in duplicate_groups:
        candidates = [
            observations_by_id[observation_id]
            for observation_id in ids
            if observation_id in observations_by_id and observation_id not in remove_ids
        ]
        if len(candidates) < 2:
            continue
        keeper = _select_observation_keeper(candidates)
        for observation_id in ids:
            if observation_id in remove_ids:
                continue
            candidate = observations_by_id.get(observation_id)
            if candidate is None or candidate is keeper:
                continue
            _merge_observation(keeper, candidate)
            remove_ids.add(observation_id)

    for note_index, note in enumerate(repaired_notes):
        note["observations"] = [
            observation
            for item_index, observation in enumerate(note.get("observations", []) or [])
            if observation_ids_by_location.get((note_index, item_index)) not in remove_ids
        ]
    return repaired_notes


def _select_observation_keeper(observations: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        observations,
        key=lambda item: (
            len(_observation_anchors(item)),
            len(str(item.get("summary") or "")),
            len(item.get("claims", []) or []),
        ),
    )


def _merge_observation(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    anchor_field = "anchor_reference_ids" if "anchor_reference_ids" in target else "anchor_block_ids"
    target[anchor_field] = _unique(_observation_anchors(target) + _observation_anchors(incoming))
    target["claims"] = _unique([str(item).strip() for item in (target.get("claims", []) or []) + (incoming.get("claims", []) or []) if str(item).strip()])
    target["related_concept_hints"] = _unique(
        [str(item).strip() for item in (target.get("related_concept_hints", []) or []) + (incoming.get("related_concept_hints", []) or []) if str(item).strip()]
    )
    for field in ("summary", "title", "query_text"):
        if len(str(incoming.get(field) or "")) > len(str(target.get(field) or "")):
            target[field] = incoming.get(field)


def _observation_anchors(observation: dict[str, Any]) -> list[str]:
    return list(
        observation.get("anchor_reference_ids")
        or observation.get("anchor_block_ids")
        or []
    )


def _renumber_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    renumbered = []
    for idx, observation in enumerate(observations, start=1):
        renumbered.append({**observation, "observation_id": f"O{idx:03d}"})
    return renumbered


def _observation_content_signature(observation: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(observation.get("type") or ""),
            str(observation.get("title") or ""),
            str(observation.get("query_text") or ""),
            str(observation.get("summary") or ""),
        ]
    )


def _apply_observation_evaluation_guards(
    evaluation: GenerationEvaluation,
    normalized: dict[str, Any],
) -> None:
    observations = normalized.get("observations", [])
    for observation in observations:
        observation_id = str(observation.get("observation_id") or "unknown")
        refs = observation.get("anchor_reference_ids", []) or []
        summary = str(observation.get("summary") or "").strip()
        title = str(observation.get("title") or "").strip()
        claims = [str(claim).strip() for claim in observation.get("claims", []) if str(claim).strip()]
        if not refs:
            _append_eval_issue(
                evaluation,
                {
                    "metric": "source_faithfulness",
                    "type": "observation_missing_ref",
                    "severity": "medium",
                    "target": [observation_id],
                    "reason": "Observation이 원문 source block anchor 없이 생성되어 검색 근거로 신뢰하기 어렵습니다.",
                    "feedback": "모든 observation은 직접 사용한 anchor_block_ids를 포함해야 합니다. 근거가 없으면 해당 observation을 제거하세요.",
                },
            )
        if _is_broken_observation_text(summary) or (not summary and not claims):
            _append_eval_issue(
                evaluation,
                {
                    "metric": "source_faithfulness",
                    "type": "broken_observation",
                    "severity": "medium",
                    "target": [observation_id],
                    "reason": "Observation summary가 중간에서 끊겼거나 검색 단위로 쓸 수 있을 만큼 완성되지 않았습니다.",
                    "feedback": "chunk 경계에서 깨진 observation은 제거하고, 같은 의미의 정상 observation만 남기세요.",
                },
            )
        if not title and not summary and not claims:
            _append_eval_issue(
                evaluation,
                {
                    "metric": "source_faithfulness",
                    "type": "broken_observation",
                    "severity": "medium",
                    "target": [observation_id],
                    "reason": "Observation에 title, summary, claims가 모두 없어 검색 단위로 사용할 수 없습니다.",
                    "feedback": "빈 observation을 생성하지 마세요.",
                },
            )

    buckets: dict[str, list[str]] = {}
    for observation in observations:
        signature = _observation_signature(observation)
        if not signature:
            continue
        buckets.setdefault(signature, []).append(str(observation.get("observation_id") or "unknown"))
    duplicates = [ids for ids in buckets.values() if len(ids) > 1]
    for ids in duplicates:
        _append_eval_issue(
            evaluation,
            {
                "metric": "source_coverage",
                "type": "duplicate_observation",
                "severity": "medium",
                "target": ids,
                "reason": "서로 다른 chunk나 registry 섹션에서 같은 의미의 observation이 중복 생성되었습니다.",
                "feedback": "query_text/resolved intent와 summary가 같은 observation은 하나로 병합하고 가장 직접적인 source block refs를 유지하세요.",
            },
        )


def _is_broken_observation_text(text: str) -> bool:
    if not text:
        return False
    stripped = text.strip()
    if stripped in {"-", "없음", "N/A"}:
        return True
    openers = {"(": ")", "[": "]", "{": "}", "“": "”", "\"": "\"", "'": "'"}
    for opener, closer in openers.items():
        if stripped.endswith(opener):
            return True
        if stripped.count(opener) > stripped.count(closer):
            return True


def _observation_signature(observation: dict[str, Any]) -> str:
    query = _compact_observation_text(str(observation.get("query_text") or ""))
    summary = _compact_observation_text(str(observation.get("summary") or ""))
    title = _compact_observation_text(str(observation.get("title") or ""))
    if query:
        return f"q:{query}"
    if summary:
        return f"s:{summary[:80]}"
    return f"t:{title}" if title else ""


def _compact_observation_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text.lower()).strip()
    text = re.sub(r"[^0-9a-z가-힣 ]+", "", text)
    return text


def _append_eval_issue(
    evaluation: GenerationEvaluation,
    issue: dict[str, Any],
) -> None:
    issues = evaluation.setdefault("issues", [])
    signature = (issue.get("type"), tuple(issue.get("target", [])))
    for existing in issues:
        if (existing.get("type"), tuple(existing.get("target", []))) == signature:
            return
    issues.append(issue)


def _unique(values: list[str]) -> list[str]:
    seen = set()
    rows = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        rows.append(value)
    return rows
