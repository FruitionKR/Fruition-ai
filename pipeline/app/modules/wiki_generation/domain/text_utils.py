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


def clean_concept_tags(tags: Iterable[str], title: str, aliases: Iterable[str], limit: int = 3) -> List[str]:
    """후보 매칭용 분류 태그를 정리한다: 공백 제거, 순서 유지 중복 제거, 제목·별칭과 같은 값 제외, 최대 limit개."""
    seen = {str(name).strip().lower() for name in [title, *aliases] if name}
    out = []
    for tag in tags or []:
        text = str(tag).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
        if len(out) >= limit:
            break
    return out
