from __future__ import annotations

import re


def clean_unit_text(text: str) -> str:
    ref_pattern = r"(?:[A-Za-z0-9_.-]+:)?B\d{4}"
    cleaned = re.sub(rf"\s*\[(?:{ref_pattern})(?:\s*,\s*(?:{ref_pattern}))*\]", "", text)
    cleaned = re.sub(r"^[-*]\s+", "", cleaned.strip())
    cleaned = re.sub(r"^(?:\[[A-Za-z0-9_,\s-]+\]\s*)+", "", cleaned)
    return cleaned.strip()
