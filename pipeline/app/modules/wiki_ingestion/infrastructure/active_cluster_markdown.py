from __future__ import annotations

import re
from typing import Any

from app.modules.wiki_ingestion.domain.unit_text import clean_unit_text


ALLOWED_CORE_RELATIONS = {
    "part_of",
    "child_of",
    "uses_or_depends_on",
    "contrasts_with",
    "supports_or_enables",
    "related_evidence",
}
MATERIALIZED_CORE_RELATIONS = ALLOWED_CORE_RELATIONS - {"related_evidence"}


def parse_active_cluster_lint(markdown: str) -> list[dict[str, Any]]:
    clusters = []
    for cluster_id, section in cluster_sections_by_id(markdown).items():
        relations, invalid_relations = cluster_relation_items(section)
        cluster = {
            "id": cluster_id,
            "representative": cluster_representative(section),
            "refs": refs_in_text(section),
            "claims": cluster_claims(section),
            "relations": relations,
            "invalid_relations": invalid_relations,
            "promotion_status": cluster_promotion_status(section),
            "promotion_source_refs": cluster_promotion_source_refs(section),
        }
        clusters.append(cluster)
    return clusters


def cluster_representative(section: str) -> str:
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("representative:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def merge_active_cluster_markdown(existing: str, incoming: str) -> str:
    existing_sections = cluster_sections_by_id(existing)
    incoming_sections = cluster_sections_by_id(incoming)
    if not existing_sections:
        return incoming
    for cluster_id, section in incoming_sections.items():
        if cluster_id not in existing_sections:
            existing_sections[cluster_id] = section
            continue
        existing_sections[cluster_id] = merge_cluster_section(existing_sections[cluster_id], section)
    lines = ["# Active Meaning Clusters"]
    for cluster_id in sorted(existing_sections):
        lines.append(existing_sections[cluster_id].strip())
    return "\n\n".join(lines).rstrip() + "\n"


def reconcile_active_cluster_invalidations(
    markdown: str,
    invalidated_refs: set[str],
    current_claim_signatures: set[tuple[str, str, str]],
    current_relation_signatures: set[tuple[str, str, str, tuple[str, ...]]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    sections = cluster_sections_by_id(markdown)
    removed_claims: list[dict[str, Any]] = []
    removed_relations: list[dict[str, Any]] = []
    changed = False
    for cluster_id, section in sections.items():
        original_section = section
        cluster = parse_active_cluster_lint(section)[0]
        stale_claim_ids = {
            str(claim.get("id") or "")
            for claim in cluster.get("claims", [])
            if invalidated_refs.intersection(claim.get("refs", []))
            and _claim_signature(cluster_id, claim) not in current_claim_signatures
        }
        if stale_claim_ids:
            section, claims = _remove_cluster_claims(
                section,
                cluster_id,
                stale_claim_ids,
            )
            removed_claims.extend(claims)
        section, relations = _remove_stale_cluster_relations(
            section,
            cluster_id,
            stale_claim_ids,
            invalidated_refs,
            current_relation_signatures,
        )
        removed_relations.extend(relations)
        changed = changed or section != original_section
        sections[cluster_id] = section
    if not changed:
        return markdown, [], []
    lines = ["# Active Meaning Clusters"]
    lines.extend(sections[cluster_id].strip() for cluster_id in sorted(sections))
    return (
        "\n\n".join(lines).rstrip() + "\n",
        removed_claims,
        removed_relations,
    )


def cluster_sections_by_id(markdown: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_id = ""
    current_lines: list[str] = []
    for line in markdown.splitlines():
        match = re.match(r"^## cluster:\s*(.+?)\s*$", line)
        if match:
            if current_id:
                sections[current_id] = "\n".join(current_lines).strip()
            current_id = match.group(1).strip()
            current_lines = [line]
            continue
        if current_id:
            current_lines.append(line)
    if current_id:
        sections[current_id] = "\n".join(current_lines).strip()
    return sections


def refs_in_text(text: str) -> list[str]:
    ref_pattern = r"[A-Za-z0-9_.-]+:B\d{4}"
    return _unique_keep_order(re.findall(ref_pattern, text))


def cluster_claims(section: str) -> list[dict[str, str]]:
    claims = []
    current_claim: dict[str, str] | None = None
    for line in section.splitlines():
        stripped = line.strip()
        match = re.match(r"^- (claim_[^:]+|ev_[^:]+):\s*(.+)$", stripped)
        if match:
            text = match.group(2)
            current_claim = {
                "id": match.group(1),
                "text": text,
                "claim": clean_unit_text(text),
                "refs": refs_in_text(text),
                "decision": "",
            }
            claims.append(current_claim)
            continue
        if current_claim and stripped.startswith("cluster_decision:"):
            current_claim["decision"] = stripped.split(":", 1)[1].strip()
    return claims


def cluster_relations(section: str) -> list[dict[str, Any]]:
    relations, _invalid = cluster_relation_items(section)
    return relations


def cluster_relation_items(section: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    relations: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_relations = False

    def finish_current() -> None:
        nonlocal current
        if not current:
            return
        target = str(current.get("target") or "").strip()
        relation = str(current.get("relation") or "").strip()
        evidence = [str(item).strip() for item in current.get("evidence", []) if str(item).strip()]
        reason = str(current.get("reason") or "").strip()
        item = {"target": target, "relation": relation, "evidence": evidence, "reason": reason}
        if target and relation in ALLOWED_CORE_RELATIONS and evidence:
            relations.append(item)
        else:
            missing = []
            if not target:
                missing.append("target")
            if relation not in ALLOWED_CORE_RELATIONS:
                missing.append("relation")
            if not evidence:
                missing.append("evidence")
            item["missing"] = missing
            invalid.append(item)
        current = None

    for line in section.splitlines():
        stripped = line.strip()
        if stripped == "### Core Relation Candidates":
            in_relations = True
            continue
        if in_relations and stripped.startswith("### "):
            finish_current()
            break
        if not in_relations:
            continue
        if stripped.startswith("- target:"):
            finish_current()
            current = {"target": stripped.split(":", 1)[1].strip(), "relation": "", "evidence": []}
        elif current and stripped.startswith("relation:"):
            current["relation"] = stripped.split(":", 1)[1].strip()
        elif current and stripped.startswith("evidence:"):
            current["evidence"] = [part.strip() for part in stripped.split("[", 1)[-1].rstrip("]").split(",") if part.strip()]
        elif current and stripped.startswith("reason:"):
            current["reason"] = stripped.split(":", 1)[1].strip()
    if in_relations:
        finish_current()
    return relations, invalid


def cluster_promotion_status(section: str) -> str:
    in_promotion = False
    for line in section.splitlines():
        stripped = line.strip()
        if stripped == "### Promotion":
            in_promotion = True
            continue
        if in_promotion and stripped.startswith("### "):
            break
        if in_promotion and stripped.startswith("status:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def cluster_promotion_source_refs(section: str) -> list[str]:
    in_promotion = False
    for line in section.splitlines():
        stripped = line.strip()
        if stripped == "### Promotion":
            in_promotion = True
            continue
        if in_promotion and stripped.startswith("### "):
            break
        if in_promotion and stripped.startswith("source_refs:"):
            return [part.strip() for part in stripped.split("[", 1)[-1].rstrip("]").split(",") if part.strip()]
    return []


def merge_cluster_section(existing: str, incoming: str) -> str:
    existing_lines = existing.splitlines()
    incoming_lines = incoming.splitlines()
    merged_lines = list(existing_lines)
    if not cluster_representative(existing):
        incoming_representative = cluster_representative(incoming)
        if incoming_representative:
            representative_index = next(
                (
                    index
                    for index, line in enumerate(merged_lines)
                    if line.strip().startswith("representative:")
                ),
                None,
            )
            if representative_index is not None:
                merged_lines[representative_index] = f"representative: {incoming_representative}"
            else:
                insert_at = next(
                    (
                        index + 1
                        for index, line in enumerate(merged_lines)
                        if line.strip().startswith("type:")
                    ),
                    1,
                )
                merged_lines.insert(insert_at, f"representative: {incoming_representative}")
    existing_claims = {line.strip() for line in existing_lines if line.strip().startswith("- claim_") or line.strip().startswith("- ev_")}
    incoming_claims = [line for line in incoming_lines if line.strip().startswith("- claim_") or line.strip().startswith("- ev_")]
    insert_at = claim_insert_index(merged_lines)
    for claim in incoming_claims:
        if claim.strip() in existing_claims:
            continue
        merged_lines.insert(insert_at, claim)
        insert_at += 1
        existing_claims.add(claim.strip())

    merge_relation_candidate_section(merged_lines, incoming_lines)
    if "### Promotion" not in existing and "### Promotion" in incoming:
        promotion = section_from_heading(incoming_lines, "### Promotion")
        if promotion:
            if merged_lines and merged_lines[-1].strip():
                merged_lines.append("")
            merged_lines.extend(promotion)
    if "### Promotion" in merged_lines:
        source_refs = _unique_keep_order(
            ref.split(":", 1)[0]
            for claim in cluster_claims("\n".join(merged_lines))
            for ref in claim.get("refs", [])
            if ":" in ref
        )
        for index, line in enumerate(merged_lines):
            if line.strip().startswith("source_refs:"):
                merged_lines[index] = f"source_refs: [{', '.join(source_refs)}]"
                break
    return "\n".join(merged_lines).strip()


def merge_heading_section(merged_lines: list[str], existing: str, incoming_lines: list[str], heading: str) -> None:
    incoming_section = section_from_heading(incoming_lines, heading)
    if not incoming_section:
        return
    if heading not in existing:
        if merged_lines and merged_lines[-1].strip():
            merged_lines.append("")
        merged_lines.extend(incoming_section)
        return
    existing_entries = {line.strip() for line in merged_lines if line.strip()}
    insert_at = len(merged_lines)
    for index, line in enumerate(merged_lines):
        if line.strip() == heading:
            insert_at = index + 1
            while insert_at < len(merged_lines) and not merged_lines[insert_at].startswith("### "):
                insert_at += 1
            break
    for line in incoming_section[1:]:
        if line.strip() and line.strip() in existing_entries:
            continue
        merged_lines.insert(insert_at, line)
        insert_at += 1
        if line.strip():
            existing_entries.add(line.strip())


def merge_relation_candidate_section(merged_lines: list[str], incoming_lines: list[str]) -> None:
    incoming_section = section_from_heading(incoming_lines, "### Core Relation Candidates")
    if not incoming_section:
        return
    existing_relations, _existing_invalid = cluster_relation_items("\n".join(merged_lines))
    incoming_relations, _incoming_invalid = cluster_relation_items("\n".join(incoming_section))
    relations_by_key = {
        relation_item_key(item): item
        for item in existing_relations
    }
    for item in incoming_relations:
        relations_by_key.setdefault(relation_item_key(item), item)
    replacement = relation_items_to_lines(list(relations_by_key.values()))
    replace_heading_section(merged_lines, "### Core Relation Candidates", replacement)


def relation_item_key(item: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    return (
        str(item.get("target") or ""),
        str(item.get("relation") or ""),
        tuple(sorted(str(evidence) for evidence in item.get("evidence", []))),
    )


def _claim_signature(
    cluster_id: str,
    claim: dict[str, Any],
) -> tuple[str, str, str]:
    return (
        cluster_id,
        str(claim.get("id") or ""),
        str(claim.get("text") or ""),
    )


def _relation_signature(
    cluster_id: str,
    relation: dict[str, Any],
) -> tuple[str, str, str, tuple[str, ...]]:
    return (
        cluster_id,
        str(relation.get("target") or ""),
        str(relation.get("relation") or ""),
        tuple(str(item) for item in relation.get("evidence", [])),
    )


def _remove_cluster_claims(
    section: str,
    cluster_id: str,
    stale_claim_ids: set[str],
) -> tuple[str, list[dict[str, Any]]]:
    claims = {
        str(claim.get("id") or ""): claim
        for claim in cluster_claims(section)
        if str(claim.get("id") or "") in stale_claim_ids
    }
    lines = section.splitlines()
    kept: list[str] = []
    skip_decision = False
    for line in lines:
        match = re.match(r"^\s*- (claim_[^:]+|ev_[^:]+):", line)
        if match and match.group(1) in claims:
            skip_decision = True
            continue
        if skip_decision and line.strip().startswith("cluster_decision:"):
            continue
        skip_decision = False
        kept.append(line)
    removed = [
        {
            "cluster_id": cluster_id,
            "claim_id": claim_id,
            "source_refs": claim.get("refs", []),
        }
        for claim_id, claim in claims.items()
    ]
    return "\n".join(kept).strip(), removed


def _remove_stale_cluster_relations(
    section: str,
    cluster_id: str,
    stale_claim_ids: set[str],
    invalidated_refs: set[str],
    current_relation_signatures: set[tuple[str, str, str, tuple[str, ...]]],
) -> tuple[str, list[dict[str, Any]]]:
    relations, invalid_relations = cluster_relation_items(section)
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed = False
    for relation in relations:
        signature = _relation_signature(cluster_id, relation)
        stale_evidence = [
            evidence
            for evidence in relation.get("evidence", [])
            if evidence in stale_claim_ids
            or (
                evidence in invalidated_refs
                and signature not in current_relation_signatures
            )
        ]
        if stale_evidence and len(stale_evidence) == len(relation.get("evidence", [])):
            removed.append({"cluster_id": cluster_id, **relation})
            changed = True
        elif stale_evidence:
            kept.append(
                {
                    **relation,
                    "evidence": [
                        evidence
                        for evidence in relation.get("evidence", [])
                        if evidence not in stale_evidence
                    ],
                }
            )
            changed = True
        else:
            kept.append(relation)
    if not changed:
        return section, []
    lines = section.splitlines()
    replace_heading_section(
        lines,
        "### Core Relation Candidates",
        relation_items_to_lines([*kept, *invalid_relations]),
    )
    return "\n".join(lines).strip(), removed


def relation_items_to_lines(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return []
    lines = ["### Core Relation Candidates"]
    for item in items:
        lines.extend(
            [
                f"- target: {item.get('target')}",
                f"  relation: {item.get('relation')}",
                f"  evidence: [{', '.join(item.get('evidence', []))}]",
                f"  reason: {item.get('reason') or '-'}",
            ]
        )
    return lines


def replace_heading_section(lines: list[str], heading: str, replacement: list[str]) -> None:
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), -1)
    if start >= 0:
        end = start + 1
        while end < len(lines) and not lines[end].startswith("### "):
            end += 1
        lines[start:end] = replacement
        return
    if not replacement:
        return
    insert_at = next((index for index, line in enumerate(lines) if line.strip() == "### Promotion"), len(lines))
    if insert_at > 0 and lines[insert_at - 1].strip():
        replacement = ["", *replacement]
    lines[insert_at:insert_at] = replacement


def claim_insert_index(lines: list[str]) -> int:
    last_claim_index = -1
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- claim_") or stripped.startswith("- ev_"):
            last_claim_index = index
    if last_claim_index >= 0:
        return last_claim_index + 1
    for index, line in enumerate(lines):
        if line.strip() == "### Evidence Claims":
            return index + 1
    return len(lines)


def section_from_heading(lines: list[str], heading: str) -> list[str]:
    out: list[str] = []
    in_section = False
    for line in lines:
        if line.strip() == heading:
            in_section = True
        elif in_section and line.startswith("### "):
            break
        if in_section:
            out.append(line)
    return out


def _unique_keep_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
