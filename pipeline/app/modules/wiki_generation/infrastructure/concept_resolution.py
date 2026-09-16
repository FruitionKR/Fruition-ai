from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from app.modules.wiki_generation.domain.text_utils import slugify, unique_keep_order


VALID_DECISIONS = {"merge_into", "link_to", "create_new"}
VALID_HINT_DECISIONS = {"merge_into_current", "merge_into_existing", "related_only", "promote_new_concept", "unresolved"}


def load_existing_concept_index(wiki_dir: str | Path | None) -> list[dict[str, Any]]:
    if not wiki_dir:
        return []
    root = Path(wiki_dir)
    if not root.exists():
        return []
    concept_dir = root / "concepts" if root.name == "wiki" else root / "wiki" / "concepts"
    if not concept_dir.exists():
        concept_dir = root / "concepts"
    if not concept_dir.exists():
        return []

    concepts = []
    for path in sorted(concept_dir.glob("*.md")):
        concepts.append(_read_concept_page(path))
    return concepts


def normalize_resolution_output(
    raw: dict[str, Any],
    incoming_concepts: list[dict[str, Any]],
    existing_concepts: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    existing_slugs = {c.get("slug") for c in existing_concepts if c.get("slug")}
    incoming_slugs = {c.get("slug") for c in incoming_concepts if c.get("slug")}
    valid_targets = existing_slugs | incoming_slugs
    by_incoming = {
        item.get("incoming_slug"): item
        for item in raw.get("resolutions", [])
        if isinstance(item, dict) and item.get("incoming_slug")
    }

    normalized = []
    for concept in incoming_concepts:
        incoming_slug = concept["slug"]
        item = by_incoming.get(incoming_slug, {})
        decision = item.get("decision") if item.get("decision") in VALID_DECISIONS else "create_new"
        canonical_slug = slugify(item.get("canonical_slug") or incoming_slug)
        link_targets = [slugify(str(target)) for target in item.get("link_targets", [])]
        link_targets = unique_keep_order([target for target in link_targets if target in valid_targets and target != incoming_slug])

        if decision in {"merge_into", "link_to"} and canonical_slug not in valid_targets:
            warnings.append(f"concept resolution ignored missing canonical slug: {incoming_slug} -> {canonical_slug}")
            decision = "create_new"
            canonical_slug = incoming_slug
        if decision == "create_new":
            canonical_slug = incoming_slug
        if decision == "link_to" and canonical_slug in valid_targets:
            link_targets = unique_keep_order([canonical_slug] + link_targets)
            canonical_slug = incoming_slug
        if decision != "merge_into" and canonical_slug != incoming_slug:
            canonical_slug = incoming_slug

        normalized.append(
            {
                "incoming_slug": incoming_slug,
                "decision": decision,
                "canonical_slug": canonical_slug,
                "alias_to_add": item.get("alias_to_add"),
                "link_targets": link_targets,
                "confidence": _float_or_zero(item.get("confidence")),
                "reason": item.get("reason", ""),
            }
        )
    return normalized


def normalize_hint_resolution_output(
    raw: dict[str, Any],
    missing_related_hints: list[dict[str, Any]],
    incoming_concepts: list[dict[str, Any]],
    existing_concepts: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    incoming_slugs = {c.get("slug") for c in incoming_concepts if c.get("slug")}
    existing_slugs = {c.get("slug") for c in existing_concepts if c.get("slug")}
    valid_targets = incoming_slugs | existing_slugs
    by_hint = {
        item.get("hint_slug"): item
        for item in raw.get("hint_resolutions", [])
        if isinstance(item, dict) and item.get("hint_slug")
    }

    normalized = []
    for hint in missing_related_hints:
        hint_slug = hint["slug"]
        item = by_hint.get(hint_slug, {})
        decision = item.get("decision") if item.get("decision") in VALID_HINT_DECISIONS else "unresolved"
        canonical_slug = slugify(item.get("canonical_slug") or "") if item.get("canonical_slug") else None
        link_targets = [slugify(str(target)) for target in item.get("link_targets", [])]
        link_targets = unique_keep_order([target for target in link_targets if target in valid_targets and target != hint_slug])

        if decision == "merge_into_current" and canonical_slug not in incoming_slugs:
            warnings.append(f"hint resolution ignored missing current canonical slug: {hint_slug} -> {canonical_slug}")
            decision = "unresolved"
            canonical_slug = None
        elif decision == "merge_into_existing" and canonical_slug not in existing_slugs:
            warnings.append(f"hint resolution ignored missing existing canonical slug: {hint_slug} -> {canonical_slug}")
            decision = "unresolved"
            canonical_slug = None
        elif decision == "related_only":
            if canonical_slug and canonical_slug not in valid_targets:
                canonical_slug = None
            if canonical_slug:
                link_targets = unique_keep_order([canonical_slug] + link_targets)
        elif decision == "promote_new_concept":
            # Promotion is recorded for review, but page creation remains disabled
            # until the product has a review policy for latent concepts.
            canonical_slug = canonical_slug or hint_slug
        elif decision == "unresolved":
            canonical_slug = None

        normalized.append(
            {
                "hint_slug": hint_slug,
                "decision": decision,
                "canonical_slug": canonical_slug,
                "link_targets": link_targets,
                "confidence": _float_or_zero(item.get("confidence")),
                "reason": item.get("reason", ""),
                "evidence_ids": hint.get("evidence_ids", []),
                "sample_claims": hint.get("sample_claims", []),
            }
        )
    return normalized


def apply_concept_resolutions(
    normalized: dict[str, Any],
    resolutions: list[dict[str, Any]],
    existing_concepts: list[dict[str, Any]],
    hint_resolutions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = copy.deepcopy(normalized)
    ledger = result.get("concept_ledger", [])
    existing_by_slug = {concept["slug"]: concept for concept in existing_concepts if concept.get("slug")}
    resolution_by_slug = {item["incoming_slug"]: item for item in resolutions}
    slug_map = {concept["slug"]: resolution_by_slug.get(concept["slug"], {}).get("canonical_slug", concept["slug"]) for concept in ledger}
    result["concept_ledger"] = merge_concept_ledger(
        ledger,
        resolution_by_slug,
        slug_map,
        existing_by_slug,
    )

    final_ledger_slugs = {concept["slug"] for concept in result["concept_ledger"]}
    existing_slugs = {concept["slug"] for concept in existing_concepts if concept.get("slug")}
    hint_map: dict[str, str] = {}
    external_related_slugs: set[str] = set()
    unresolved_hints: list[dict[str, Any]] = []
    for item in hint_resolutions or []:
        hint_slug = item["hint_slug"]
        canonical_slug = item.get("canonical_slug")
        if item.get("decision") == "merge_into_current" and canonical_slug in final_ledger_slugs:
            hint_map[hint_slug] = canonical_slug
        elif item.get("decision") == "merge_into_existing" and canonical_slug in existing_slugs:
            hint_map[hint_slug] = canonical_slug
            external_related_slugs.add(canonical_slug)
        elif item.get("decision") == "related_only":
            for target_slug in item.get("link_targets", []):
                if target_slug in final_ledger_slugs:
                    hint_map[hint_slug] = target_slug
                    break
                if target_slug in existing_slugs:
                    hint_map[hint_slug] = target_slug
                    external_related_slugs.add(target_slug)
                    break
        elif item.get("decision") in {"promote_new_concept", "unresolved"}:
            unresolved_hints.append(item)

    for evidence in result.get("evidence_units", []):
        mapped_slugs = []
        for slug in evidence.get("related_concept_slugs", []):
            mapped = slug_map.get(slug, hint_map.get(slug, slug))
            mapped_slugs.append(mapped)
            if mapped in final_ledger_slugs:
                for concept in result["concept_ledger"]:
                    if concept["slug"] == mapped and evidence["evidence_id"] not in concept.get("evidence_claim_ids", []):
                        concept.setdefault("evidence_claim_ids", []).append(evidence["evidence_id"])
                        concept["importance_score"] = concept.get("importance_score", 0) + 2 * max(0.0, min(1.0, evidence.get("confidence", 0.0)))
                        break
        evidence["related_concept_slugs"] = unique_keep_order(mapped_slugs)
    result["concept_resolutions"] = resolutions
    result["hint_resolutions"] = hint_resolutions or []
    result["concept_slug_map"] = slug_map
    result["hint_slug_map"] = hint_map
    result["external_related_concept_slugs"] = sorted(external_related_slugs)
    result["unresolved_related_concept_hints"] = unresolved_hints
    result["warnings"] = _remaining_related_hint_warnings(result, final_ledger_slugs | existing_slugs)
    result["existing_concept_index"] = existing_concepts
    return result


def merge_concept_ledger(
    ledger: list[dict[str, Any]],
    resolution_by_slug: dict[str, dict[str, Any]],
    slug_map: dict[str, str],
    existing_by_slug: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged_by_slug: dict[str, dict[str, Any]] = {}
    for concept in ledger:
        original_slug = concept["slug"]
        resolution = resolution_by_slug.get(original_slug, {})
        canonical_slug = slug_map.get(original_slug, original_slug)
        target = merged_by_slug.get(canonical_slug)
        if target is None:
            target = copy.deepcopy(concept)
            target["slug"] = canonical_slug
            if resolution.get("decision") == "merge_into" and canonical_slug in existing_by_slug:
                existing = existing_by_slug[canonical_slug]
                target["title"] = existing.get("title") or target.get("title")
                if existing.get("summary") and not target.get("definition"):
                    target["definition"] = existing["summary"]
            target["aliases"] = unique_keep_order(target.get("aliases", []) + [original_slug])
            merged_by_slug[canonical_slug] = target
        else:
            target["aliases"] = unique_keep_order(target.get("aliases", []) + concept.get("aliases", []) + [concept.get("title"), original_slug])
            target["anchor_reference_ids"] = unique_keep_order(target.get("anchor_reference_ids", []) + concept.get("anchor_reference_ids", []))
            target["mention_reference_ids"] = unique_keep_order(target.get("mention_reference_ids", []) + concept.get("mention_reference_ids", []))
            target["display_reference_ids"] = unique_keep_order(target.get("display_reference_ids", []) + concept.get("display_reference_ids", []))
            target["source_document_ids"] = unique_keep_order(target.get("source_document_ids", []) + concept.get("source_document_ids", []))
            target["evidence_claim_ids"] = unique_keep_order(target.get("evidence_claim_ids", []) + concept.get("evidence_claim_ids", []))
            target["mention_count"] = len(target.get("mention_reference_ids", []))
            target["importance_score"] = target.get("importance_score", 0) + concept.get("importance_score", 0)
            if len(concept.get("definition", "")) > len(target.get("definition", "")):
                target["definition"] = concept.get("definition", "")
            if len(concept.get("why_page_worthy", "")) > len(target.get("why_page_worthy", "")):
                target["why_page_worthy"] = concept.get("why_page_worthy", "")

        alias_to_add = resolution.get("alias_to_add")
        if alias_to_add:
            target["aliases"] = unique_keep_order(target.get("aliases", []) + [alias_to_add])
    return sorted(merged_by_slug.values(), key=lambda concept: (-concept.get("importance_score", 0), concept.get("slug", "")))


def _remaining_related_hint_warnings(normalized: dict[str, Any], known_slugs: set[str]) -> list[str]:
    warnings = list(normalized.get("warnings", []))
    for evidence in normalized.get("evidence_units", []):
        for slug in evidence.get("related_concept_slugs", []):
            if slug not in known_slugs:
                warnings.append(f"evidence {evidence['evidence_id']} references unresolved concept slug: {slug}")
    return warnings


def _read_concept_page(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    frontmatter = _frontmatter(text)
    title = frontmatter.get("title") or _first_heading(text) or path.stem
    slug = slugify(frontmatter.get("slug") or path.stem)
    return {
        "slug": slug,
        "title": title,
        "aliases": _aliases(frontmatter),
        "summary": _definition_summary(text),
        "path": str(path),
    }


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    values: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    return values


def _first_heading(text: str) -> str | None:
    match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _definition_summary(text: str) -> str:
    match = re.search(r"^##\s+Definition\s*\n(.+?)(?:\n##\s+|\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    lines = [line.strip() for line in match.group(1).splitlines() if line.strip() and not line.strip().startswith("-")]
    return " ".join(lines)


def _aliases(frontmatter: dict[str, str]) -> list[str]:
    raw = frontmatter.get("aliases") or ""
    return [item.strip().strip("'\"") for item in raw.strip("[]").split(",") if item.strip()]


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0
