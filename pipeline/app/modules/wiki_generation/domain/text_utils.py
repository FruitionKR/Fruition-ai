from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable, List


def sha1_short(text: str, n: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).strip().lower()
    text = re.sub(r"[`'\"()\[\]{}]", "", text)
    text = re.sub(r"[^a-z0-9가-힣]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    # Keep English slugs where possible. Korean-only title still becomes valid but less ideal.
    if len(text) > 255:
        text = f"{text[:246]}-{sha1_short(text)}"
    return text or "untitled"


def unique_keep_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if not item:
            continue
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def contains_ci(text: str, needle: str) -> bool:
    return needle.strip().lower() in text.lower()
