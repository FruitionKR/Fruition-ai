from __future__ import annotations

from typing import Any

from app.modules.wiki_ingestion.domain.contribution_identity import (
    globalize_contribution_identity,
)


def build_concept_contributions(
    *,
    operation_id: str,
    normalized: dict[str, Any],
    source_blocks: list[dict[str, Any]],
    links: list[dict[str, Any]],
    source_key_points: list[dict[str, Any]] | None = None,
    concept_update_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    document_id = str(normalized.get("document", {}).get("document_id") or "")
    evidence_by_id = {
        str(item.get("evidence_id")): item
        for item in normalized.get("evidence_units", [])
        if isinstance(item, dict) and item.get("evidence_id")
    }
    blocks_by_id = {
        str(item.get("block_id")): item
        for item in source_blocks
        if isinstance(item, dict) and item.get("block_id")
    }
    key_point_inputs = (
        source_key_points
        if source_key_points is not None
        else _normalized_source_key_points(normalized)
    )
    contributions: dict[str, dict[str, Any]] = {}
    for concept in normalized.get("concept_ledger", []):
        if not isinstance(concept, dict):
            continue
        slug = str(concept.get("slug") or "")
        if not slug:
            continue
        evidence_units = [
            evidence_by_id[evidence_id]
            for evidence_id in concept.get("evidence_claim_ids", [])
            if evidence_id in evidence_by_id
        ]
        block_ids = _concept_block_ids(concept, evidence_units)
        concept_source_key_points = [
            item
            for item in key_point_inputs
            if set(item.get("anchor_reference_ids", [])).intersection(block_ids)
        ]
        contributions[slug] = globalize_contribution_identity(
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "document_id": document_id,
                "concept": concept,
                "evidence_units": evidence_units,
                "source_blocks": [
                    blocks_by_id[block_id]
                    for block_id in block_ids
                    if block_id in blocks_by_id
                ],
                "source_key_points": concept_source_key_points,
                "links": [
                    link
                    for link in links
                    if _link_supports_concept(link, slug, document_id)
                ],
            }
        )
    update_contributions = _concept_update_contributions(
        operation_id=operation_id,
        document_id=document_id,
        existing_concepts=normalized.get("existing_concept_index", []),
        decisions=concept_update_decisions or [],
        source_blocks=source_blocks,
        links=links,
    )
    for slug, update in update_contributions.items():
        existing = contributions.get(slug)
        contributions[slug] = (
            _merge_contribution(existing, update) if existing else update
        )
    return contributions


def _concept_update_contributions(
    *,
    operation_id: str,
    document_id: str,
    existing_concepts: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    source_blocks: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    concepts_by_slug = {
        str(concept.get("slug") or ""): concept
        for concept in existing_concepts
        if isinstance(concept, dict) and concept.get("slug")
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for decision in decisions:
        if not isinstance(decision, dict) or decision.get("decision") != "same_concept":
            continue
        slug = str(decision.get("concept_slug") or "")
        claim = str(decision.get("claim") or "")
        if slug and claim:
            grouped.setdefault(slug, []).append(decision)

    contributions: dict[str, dict[str, Any]] = {}
    for slug, values in grouped.items():
        evidence_units = [
            {
                "evidence_id": str(
                    value.get("claim_id") or f"claim-{index}"
                ),
                "claim": str(value["claim"]),
                "anchor_reference_ids": list(value.get("refs", [])),
                "related_concept_slugs": [slug],
                "source_document_id": document_id,
            }
            for index, value in enumerate(values, start=1)
        ]
        refs = list(
            dict.fromkeys(
                str(ref)
                for value in values
                for ref in value.get("refs", [])
                if ref
            )
        )
        source_block_ids = {ref.rsplit(":", 1)[-1] for ref in refs}
        existing_concept = concepts_by_slug.get(slug, {})
        contributions[slug] = globalize_contribution_identity(
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "document_id": document_id,
                "concept": {
                    "slug": slug,
                    "title": str(existing_concept.get("title") or slug),
                    "definition": "",
                    "anchor_reference_ids": refs,
                    "source_document_ids": [document_id],
                    "evidence_claim_ids": [
                        unit["evidence_id"] for unit in evidence_units
                    ],
                },
                "evidence_units": evidence_units,
                "source_blocks": [
                    block
                    for block in source_blocks
                    if str(block.get("block_id") or "") in source_block_ids
                ],
                "source_key_points": [],
                "links": [
                    link
                    for link in links
                    if _link_supports_concept(link, slug, document_id)
                ],
            }
        )
    return contributions


def _merge_contribution(
    base: dict[str, Any],
    update: dict[str, Any],
) -> dict[str, Any]:
    concept = dict(base["concept"])
    for field in (
        "anchor_reference_ids",
        "source_document_ids",
        "evidence_claim_ids",
    ):
        concept[field] = list(
            dict.fromkeys(
                [*concept.get(field, []), *update["concept"].get(field, [])]
            )
        )
    return {
        **base,
        "concept": concept,
        "evidence_units": _unique_dicts(
            [*base.get("evidence_units", []), *update.get("evidence_units", [])],
            "evidence_id",
        ),
        "source_blocks": _unique_dicts(
            [*base.get("source_blocks", []), *update.get("source_blocks", [])],
            "block_id",
        ),
        "links": _unique_dicts(
            [*base.get("links", []), *update.get("links", [])],
            "relation",
        ),
    }


def _unique_dicts(
    values: list[dict[str, Any]],
    identity_field: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for value in values:
        key = (
            value.get(identity_field),
            value.get("source"),
            value.get("target"),
        )
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _normalized_source_key_points(
    normalized: dict[str, Any],
) -> list[dict[str, Any]]:
    key_points: list[dict[str, Any]] = []
    for note in normalized.get("semantic_notes", []):
        for item in note.get("key_points", []):
            if isinstance(item, dict):
                key_points.append(item)
    return key_points


def _concept_block_ids(
    concept: dict[str, Any],
    evidence_units: list[dict[str, Any]],
) -> list[str]:
    values = [
        *concept.get("anchor_reference_ids", []),
        *concept.get("mention_reference_ids", []),
        *concept.get("display_reference_ids", []),
        *(
            block_id
            for evidence in evidence_units
            for block_id in evidence.get("anchor_reference_ids", [])
        ),
    ]
    return list(dict.fromkeys(str(value) for value in values if value))


def _link_supports_concept(
    link: dict[str, Any],
    concept_slug: str,
    document_id: str,
) -> bool:
    source = str(link.get("source") or "")
    target = str(link.get("target") or "")
    concept_ref = f"concept:{concept_slug}"
    return source == concept_ref or (
        source == f"source:{document_id}" and target == concept_ref
    )
