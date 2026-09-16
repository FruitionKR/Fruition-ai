from __future__ import annotations

import hashlib
import re
from typing import Any

from app.modules.wiki_ingestion.domain.unit_text import clean_unit_text


def extract_embedding_units(markdown: str) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    current_section = "body"
    in_frontmatter = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter or not line:
            continue
        heading = re.match(r"^##+\s+(.+?)\s*$", line)
        if heading:
            current_section = heading.group(1).strip()
            continue
        if not source_block_ids(line):
            continue
        unit_text = clean_unit_text(line)
        if not unit_text:
            continue
        units.append(
            {
                "unit_type": unit_type(current_section),
                "block_refs": source_block_ids(line),
                "text": unit_text,
                "weight": section_weight(current_section),
            }
        )
    return dedupe_units(units)


def dedupe_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for unit in units:
        key = (unit["unit_type"], tuple(unit["block_refs"]), unit["text"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(unit)
    return deduped


def unit_type(section: str) -> str:
    normalized = section.strip().lower()
    mapping = {
        "key points": "key_point",
        "observations": "observation",
        "observation": "observation",
        "categories": "category",
        "core concepts": "core_concept",
        "section candidates": "section_candidate",
        "mentions": "mention",
    }
    return mapping.get(normalized, "source_block")


def section_weight(section: str) -> float:
    weights = {
        "key_point": 1.35,
        "observation": 1.30,
        "core_concept": 1.20,
        "section_candidate": 1.15,
        "mention": 1.05,
        "category": 0.95,
    }
    return weights.get(unit_type(section), 1.0)


def source_block_ids(text: str) -> list[str]:
    block_ids = []
    ref_pattern = r"(?:[A-Za-z0-9_.-]+:)?B\d{4}"
    for group in re.findall(rf"\[((?:{ref_pattern})(?:\s*,\s*(?:{ref_pattern}))*)\]", text):
        block_ids.extend(part.strip() for part in group.split(",") if part.strip())
    return list(dict.fromkeys(block_ids))


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
