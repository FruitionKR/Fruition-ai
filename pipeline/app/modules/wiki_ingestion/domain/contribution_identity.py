from __future__ import annotations

from typing import Any


CONCEPT_REFERENCE_FIELDS = (
    "anchor_reference_ids",
    "mention_reference_ids",
    "display_reference_ids",
)


def globalize_contribution_identity(
    contribution: dict[str, Any],
) -> dict[str, Any]:
    document_id = str(contribution.get("document_id") or "")
    operation_id = str(contribution.get("operation_id") or "")

    concept = dict(contribution.get("concept") or {})
    for field in CONCEPT_REFERENCE_FIELDS:
        concept[field] = _global_refs(document_id, concept.get(field, []))
    concept["evidence_claim_ids"] = [
        _global_evidence_id(operation_id, evidence_id)
        for evidence_id in concept.get("evidence_claim_ids", [])
        if evidence_id
    ]

    evidence_units = []
    for value in contribution.get("evidence_units", []):
        if not isinstance(value, dict):
            continue
        evidence = dict(value)
        source_document_id = str(
            evidence.get("source_document_id") or document_id
        )
        evidence["evidence_id"] = _global_evidence_id(
            operation_id,
            evidence.get("evidence_id"),
        )
        evidence["anchor_reference_ids"] = _global_refs(
            source_document_id,
            evidence.get("anchor_reference_ids", []),
        )
        evidence_units.append(evidence)

    source_key_points = []
    for value in contribution.get("source_key_points", []):
        if not isinstance(value, dict):
            continue
        item = dict(value)
        item["anchor_reference_ids"] = _global_refs(
            document_id,
            item.get("anchor_reference_ids", []),
        )
        source_key_points.append(item)

    return {
        **contribution,
        "concept": concept,
        "evidence_units": evidence_units,
        "source_key_points": source_key_points,
    }


def _global_refs(document_id: str, refs: list[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            str(ref)
            if ":" in str(ref) or not document_id
            else f"{document_id}:{ref}"
            for ref in refs
            if ref
        )
    )


def _global_evidence_id(operation_id: str, evidence_id: Any) -> str:
    value = str(evidence_id or "")
    if not operation_id or not value:
        return value
    prefix = f"{operation_id}:"
    return value if value.startswith(prefix) else f"{prefix}{value}"
