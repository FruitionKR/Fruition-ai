from __future__ import annotations

from typing import Any

from app.modules.wiki_generation.domain.text_utils import unique_keep_order
from app.modules.wiki_generation.infrastructure.ref_format import cite_refs


def collect_source_key_points(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    key_points = []
    seen = set()
    for note in normalized.get("semantic_notes", []):
        for item in note.get("key_points", []):
            text = str(item.get("text", "")).strip()
            refs = item_refs(item)
            if not text or text in seen:
                continue
            seen.add(text)
            key_points.append({"text": text, "anchor_reference_ids": refs})
    return key_points


def item_refs(item: dict[str, Any]) -> list[str]:
    return unique_keep_order(item.get("anchor_reference_ids", []) or item.get("anchor_block_ids", []))


def concept_evidence(concept: dict[str, Any], evidence_units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slug = concept.get("slug")
    direct = [ev for ev in evidence_units if slug and slug in ev.get("related_concept_slugs", [])]
    if direct:
        return direct
    concept_refs = _concept_base_refs(concept)
    if not concept_refs:
        return []
    fallback = []
    for ev in evidence_units:
        ev_refs = set(ev.get("anchor_reference_ids", []))
        if concept_refs.intersection(ev_refs):
            fallback.append(ev)
    return fallback


def concept_key_points_from_source(
    concept: dict[str, Any],
    related_evidence: list[dict[str, Any]],
    source_key_points: list[dict[str, Any]],
    document_id: str,
) -> list[str]:
    concept_refs = _concept_refs(concept, related_evidence)
    lines = []
    seen = set()
    used_refs: set[str] = set()
    for item in source_key_points:
        refs = item_refs(item)
        text = str(item.get("text", "")).strip()
        if not text or text in seen:
            continue
        if concept_refs and not concept_refs.intersection(refs):
            continue
        if refs and used_refs.intersection(refs):
            continue
        seen.add(text)
        used_refs.update(refs)
        lines.append(f"- {text}{cite_refs(refs, document_id)}")
    return lines


def concept_related_lines(
    slug: str,
    normalized: dict[str, Any],
    ledger_by_slug: dict[str, dict[str, Any]],
    source_key_points: list[dict[str, Any]] | None = None,
) -> list[str]:
    related_slugs: list[str] = []
    for ev in normalized.get("evidence_units", []):
        ev_slugs = [s for s in ev.get("related_concept_slugs", []) if s in ledger_by_slug]
        if slug in ev_slugs:
            related_slugs.extend([s for s in ev_slugs if s != slug])

    related_slugs.extend(
        target
        for source, target, _reason in shared_key_point_pairs(normalized, ledger_by_slug, source_key_points or [])
        if source == slug
    )
    related_slugs.extend(
        source
        for source, target, _reason in shared_key_point_pairs(normalized, ledger_by_slug, source_key_points or [])
        if target == slug
    )

    for resolution in normalized.get("concept_resolutions", []):
        source_slug = resolution.get("canonical_slug") or resolution.get("incoming_slug")
        targets = [target for target in resolution.get("link_targets", []) if target in ledger_by_slug]
        if source_slug == slug:
            related_slugs.extend(targets)
        elif slug in targets and source_slug in ledger_by_slug:
            related_slugs.append(source_slug)

    for resolution in normalized.get("hint_resolutions", []):
        source_slug = resolution.get("canonical_slug")
        targets = [target for target in resolution.get("link_targets", []) if target in ledger_by_slug]
        if source_slug == slug:
            related_slugs.extend(targets)
        elif slug in targets and source_slug in ledger_by_slug:
            related_slugs.append(source_slug)

    lines = []
    for target_slug in unique_keep_order([s for s in related_slugs if s and s != slug]):
        target = ledger_by_slug.get(target_slug)
        if target:
            lines.append(f"- [[{target_slug}|{target.get('title') or target_slug}]]")
    return lines


def shared_key_point_pairs(
    normalized: dict[str, Any],
    ledger_by_slug: dict[str, dict[str, Any]],
    source_key_points: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    concepts = list(ledger_by_slug.values())
    evidence = normalized.get("evidence_units", [])
    concept_refs_by_slug = {
        concept["slug"]: _concept_refs(concept, concept_evidence(concept, evidence))
        for concept in concepts
    }
    pairs: list[tuple[str, str, str]] = []
    seen = set()
    for item in source_key_points or collect_source_key_points(normalized):
        refs = set(item_refs(item))
        if not refs:
            continue
        matched_slugs = sorted(
            slug
            for slug, concept_refs in concept_refs_by_slug.items()
            if concept_refs.intersection(refs)
        )
        for i, source_slug in enumerate(matched_slugs):
            for target_slug in matched_slugs[i + 1 :]:
                pair = (source_slug, target_slug)
                if pair in seen:
                    continue
                seen.add(pair)
                pairs.append((source_slug, target_slug, "shared_source_key_point"))
    return pairs


def _concept_refs(concept: dict[str, Any], related_evidence: list[dict[str, Any]]) -> set[str]:
    refs: list[str] = []
    refs.extend(concept.get("display_reference_ids", []))
    refs.extend(concept.get("anchor_reference_ids", []))
    refs.extend(concept.get("mention_reference_ids", []))
    for ev in related_evidence:
        refs.extend(ev.get("anchor_reference_ids", []))
    return set(unique_keep_order(refs))


def _concept_base_refs(concept: dict[str, Any]) -> set[str]:
    refs: list[str] = []
    refs.extend(concept.get("display_reference_ids", []))
    refs.extend(concept.get("anchor_reference_ids", []))
    refs.extend(concept.get("mention_reference_ids", []))
    return set(unique_keep_order(refs))
