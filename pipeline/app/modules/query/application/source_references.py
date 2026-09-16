from __future__ import annotations

import re

from app.modules.query.domain.entities import SourceReference


_REF_PATTERN = r"(?:[A-Za-z0-9_.-]+:)?B\d{4}"


def has_global_source_refs(text: str | None) -> bool:
    return bool(text and re.search(r"\[[^\]]*[A-Za-z0-9_.-]+:B\d{4}", text))


def source_block_ids(text: str) -> list[str]:
    block_ids: list[str] = []
    for group in re.findall(rf"\[((?:{_REF_PATTERN})(?:\s*,\s*(?:{_REF_PATTERN}))*)\]", text):
        for raw_ref in group.split(","):
            ref = raw_ref.strip()
            block_ids.append(ref.split(":", 1)[-1])
    return list(dict.fromkeys(block_ids))


def source_references(text: str, default_document_id: str | None) -> list[SourceReference]:
    refs: list[SourceReference] = []
    for group in re.findall(rf"\[((?:{_REF_PATTERN})(?:\s*,\s*(?:{_REF_PATTERN}))*)\]", text):
        refs.extend(source_references_from_ids(group.split(","), default_document_id))
    return dedupe_source_refs(refs)


def source_references_from_ids(
    ref_ids: list[str],
    default_document_id: str | None,
) -> list[SourceReference]:
    refs: list[SourceReference] = []
    for raw_ref in ref_ids:
        ref = raw_ref.strip()
        if not ref or ref == "web":
            continue
        if ":" in ref:
            document_id, block_id = ref.split(":", 1)
        else:
            document_id, block_id = default_document_id, ref
        if document_id and re.fullmatch(r"B\d{4}", block_id):
            refs.append(SourceReference(source_document_id=document_id, source_block_id=block_id))
    return dedupe_source_refs(refs)


def legacy_source_fields(
    refs: list[SourceReference],
    default_document_id: str,
) -> tuple[str, list[str]]:
    if not refs:
        return default_document_id, []
    primary_document_id = refs[0].source_document_id
    return primary_document_id, [
        ref.source_block_id
        for ref in refs
        if ref.source_document_id == primary_document_id
    ]


def dedupe_source_refs(refs: list[SourceReference]) -> list[SourceReference]:
    deduped: list[SourceReference] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        key = (ref.source_document_id, ref.source_block_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped


def remove_block_refs(text: str, *, strip: bool = True) -> str:
    """블록 참조를 걷어낸다.

    evidence 문장은 앞뒤 공백까지 다듬어야 해서 기본값이 strip=True다. 답변 본문처럼
    원문 공백이 의미를 갖는(들여쓰기 코드블록 등) 입력은 strip=False로 부른다.
    """
    # 참조 앞은 가로 공백만 걷는다. \s*로 개행까지 먹으면 줄 맨 앞 참조를 지울 때
    # 앞의 빈 줄이 함께 사라져 제목·문단이 붙어버린다.
    cleaned = re.sub(rf"[ \t]*\[(?:{_REF_PATTERN})(?:\s*,\s*(?:{_REF_PATTERN}))*\]", "", text)
    return cleaned.strip() if strip else cleaned


def is_block_ref_only(text: str) -> bool:
    return bool(re.fullmatch(rf"(?:\[(?:{_REF_PATTERN})(?:\s*,\s*(?:{_REF_PATTERN}))*\]\s*)+", text.strip()))
