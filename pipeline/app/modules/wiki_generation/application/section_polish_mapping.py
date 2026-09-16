from __future__ import annotations

import re
from typing import Any


def map_polish_output(raw: dict[str, Any], source_blocks: list[Any], warnings: list[str], context: str) -> dict[str, Any]:
    valid_bids = {block.block_id for block in source_blocks}

    def map_refs(anchor_block_ids: list[str]) -> list[str]:
        refs = []
        for bid in anchor_block_ids or []:
            if bid not in valid_bids:
                warnings.append(f"{context}: unknown polish anchor_block_id {bid}")
                continue
            refs.append(bid)
        return refs

    mapped = {
        "section": raw.get("section"),
        "text": _clean_polish_text(raw.get("text", "")),
        "anchor_reference_ids": map_refs(raw.get("anchor_block_ids", [])),
        "items": [],
        "related_concept_hints": raw.get("related_concept_hints", []),
        "confidence": raw.get("confidence", 0.0),
    }
    for item in raw.get("items", []) or []:
        mapped["items"].append(
            {
                "text": _clean_polish_text(item.get("text", "")),
                "anchor_reference_ids": map_refs(item.get("anchor_block_ids", [])),
            }
        )
    return mapped


def _clean_polish_text(text: Any) -> str:
    text = str(text or "")
    text = re.sub(r"\s*[\[(]B\d{4}[\])]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
