from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable, List


def sha1_short(text: str, n: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# 페이지 본문이 문장 뒤에 붙이는 근거 표시 묶음(`[doc_x:B0003, doc_y:B0001]`, ref_format.cite_refs).
_CITATION_REF = r"[\w.\-]+:[A-Za-z][\w.\-]*"
_CITATION_GROUP = re.compile(rf"\s*\[{_CITATION_REF}(?:\s*,\s*{_CITATION_REF})*\](?!\()")


def strip_citation_groups(text: str) -> str:
    """본문에서 읽은 문장의 인라인 근거 표시 묶음을 뺀다.

    본문 Markdown을 다시 개념 값(정의 등)으로 쓰면 조립기가 근거 표시를 또 붙여 같은 근거가 두 번 보인다.
    """
    return _CITATION_GROUP.sub("", str(text or "")).strip()


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
    """후보 매칭용 분류 태그를 정리한다: 쉼표·공백 정리, 순서 유지 중복 제거, 제목·별칭과 같은 값 제외, 최대 limit개."""
    seen = {str(name).strip().lower() for name in [title, *aliases] if name}
    out = []
    for tag in tags or []:
        # frontmatter에 쉼표로 이어 쓰므로 태그 안의 쉼표는 공백으로 바꾼다.
        text = " ".join(str(tag).replace(",", " ").split())
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
        if len(out) >= limit:
            break
    return out


def name_key(name: object) -> str:
    """이름 일치 비교용 키. 대소문자·전각/반각·공백·하이픈·밑줄만 맞추고 기호는 남긴다."""
    return " ".join(unicodedata.normalize("NFKC", str(name)).casefold().replace("-", " ").replace("_", " ").split())


# 언급의 정의로 쓰는 원문 길이. 개념 갱신·클러스터 후보 문장 한 줄 분량이다.
MENTION_SOURCE_TEXT_CHARS = 300


def mention_source_text(name: str, block_text: str) -> str:
    """언급의 정의. 언급은 설명을 받지 않으므로 근거 블록에서 언급이 나온 문장을 앞에 둔 원문 300자를 쓴다.

    클러스터 판정과 하위 항목 배정(part_of)이 이 원문으로 판단한다. 이름이 블록에 없으면 블록 앞부분을 쓴다.
    """
    text = " ".join(block_text.split())
    at = text.lower().find(name.strip().lower()) if name.strip() else -1
    if at < 0:
        return text[:MENTION_SOURCE_TEXT_CHARS]
    # 언급이 든 문장의 시작. 문장이 길면 언급을 가운데 둔다.
    sentence_start = max(
        (text.rfind(mark, 0, at) + len(mark) for mark in (". ", "? ", "! ") if text.rfind(mark, 0, at) >= 0), default=0
    )
    start = max(sentence_start, at - MENTION_SOURCE_TEXT_CHARS // 2)
    return text[start:start + MENTION_SOURCE_TEXT_CHARS].strip()
