from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.modules.wiki_generation.domain.text_utils import name_key, slugify, unique_keep_order


class ConceptSlugExhausted(RuntimeError):
    pass


def identity_aliases(concept: dict[str, Any]) -> list[str]:
    """Known identity aliases only. Legacy markdown aliases are display/link compatibility data."""
    return [str(alias) for alias in concept.get("identity_aliases") or [] if str(alias).strip()]


def concept_name_keys(concept: dict[str, Any]) -> set[str]:
    return {
        name_key(name)
        for name in [concept.get("title"), *identity_aliases(concept)]
        if name and name_key(name)
    }


def concept_slug_candidates(preferred: object, fallback: object | None = None) -> tuple[str, ...]:
    """Existing split rule: preferred, optional fallback, then fallback/base-2 through -9."""
    first = slugify(str(preferred))
    base = slugify(str(fallback if fallback is not None else preferred))
    return tuple(
        unique_keep_order(
            [
                first,
                *([base] if fallback is not None else []),
                *(slugify(f"{base}-{number}") for number in range(2, 10)),
            ]
        )
    )


def available_concept_slug(
    preferred: object,
    occupied: Iterable[str],
) -> str:
    occupied_slugs = set(occupied)
    slug = next(
        (
            candidate
            for candidate in concept_slug_candidates(preferred)
            if candidate != "untitled" and candidate not in occupied_slugs
        ),
        None,
    )
    if slug is None:
        raise ConceptSlugExhausted(f"concept slug candidates exhausted: {slugify(str(preferred))}")
    return slug


def remap_concept_markdown_slugs(markdown: str, slug_map: dict[str, str]) -> str:
    lines = []
    for line in markdown.splitlines():
        if line.startswith("slug:"):
            slug = line.split(":", 1)[1].strip()
            line = f"slug: {slug_map.get(slug, slug)}"
        lines.append(line)
    text = "\n".join(lines) + ("\n" if markdown.endswith("\n") else "")
    text = re.sub(
        r"\[\[([^\]|#]+)",
        lambda match: f"[[{slug_map.get(match.group(1), match.group(1))}",
        text,
    )
    return re.sub(
        r"concept:([a-z0-9가-힣-]+)",
        lambda match: f"concept:{slug_map.get(match.group(1), match.group(1))}",
        text,
    )


def remap_normalized_concept_slugs(
    normalized: dict[str, Any],
    slug_map: dict[str, str],
) -> None:
    """Apply one concept slug allocation map to normalized references in place."""
    if not slug_map:
        return

    def remap_ref(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if value.startswith("concept:"):
            slug = value.split(":", 1)[1]
            return f"concept:{slug_map.get(slug, slug)}"
        return slug_map.get(value, value)

    for concept in normalized.get("concept_ledger", []):
        concept["slug"] = slug_map.get(concept.get("slug"), concept.get("slug"))
    for evidence in normalized.get("evidence_units", []):
        evidence["related_concept_slugs"] = unique_keep_order(
            slug_map.get(slug, slug)
            for slug in evidence.get("related_concept_slugs", [])
        )
    for observation in normalized.get("observations", []):
        if "related_concept_hints" in observation:
            observation["related_concept_hints"] = unique_keep_order(
                slug_map.get(slug, slug)
                for slug in observation.get("related_concept_hints", [])
            )

    entry_changes = normalized.get("entry_changes")
    if isinstance(entry_changes, dict):
        remapped_changes: dict[str, list[dict[str, Any]]] = {}
        for slug, changes in entry_changes.items():
            remapped_changes.setdefault(slug_map.get(slug, slug), []).extend(
                {
                    **change,
                    **(
                        {"slug": slug_map.get(change["slug"], change["slug"])}
                        if change.get("slug")
                        else {}
                    ),
                }
                for change in changes
            )
        normalized["entry_changes"] = remapped_changes
    for field in ("concept_slug_map", "hint_slug_map"):
        if field in normalized:
            normalized[field] = {
                source: slug_map.get(target, target)
                for source, target in normalized.get(field, {}).items()
            }
    for resolution in [
        *normalized.get("concept_resolutions", []),
        *normalized.get("hint_resolutions", []),
    ]:
        if resolution.get("incoming_slug"):
            resolution["incoming_slug"] = slug_map.get(
                resolution["incoming_slug"], resolution["incoming_slug"]
            )
        if resolution.get("canonical_slug"):
            resolution["canonical_slug"] = slug_map.get(
                resolution["canonical_slug"], resolution["canonical_slug"]
            )
        if "link_targets" in resolution:
            resolution["link_targets"] = unique_keep_order(
                slug_map.get(slug, slug)
                for slug in resolution.get("link_targets", [])
            )
        if resolution.get("link_confidences"):
            confidences: dict[str, Any] = {}
            for slug, confidence in resolution["link_confidences"].items():
                target = slug_map.get(slug, slug)
                if target not in confidences or confidence > confidences[target]:
                    confidences[target] = confidence
            resolution["link_confidences"] = confidences
    for field in ("entry_references", "alias_concept_links", "links"):
        for link in normalized.get(field, []):
            for endpoint in ("source", "target"):
                if endpoint in link:
                    link[endpoint] = remap_ref(link[endpoint])
    for decision in normalized.get("concept_update_decisions", []):
        if decision.get("concept_slug"):
            decision["concept_slug"] = slug_map.get(
                decision["concept_slug"], decision["concept_slug"]
            )
    for field in ("external_related_concept_slugs",):
        if field in normalized:
            normalized[field] = unique_keep_order(
                slug_map.get(slug, slug) for slug in normalized.get(field, [])
            )
    for field in ("missing_related_concept_hints", "unresolved_related_concept_hints"):
        for hint in normalized.get(field, []):
            if isinstance(hint, dict) and hint.get("slug"):
                hint["slug"] = slug_map.get(hint["slug"], hint["slug"])
