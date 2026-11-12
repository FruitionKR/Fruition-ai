"""승인 화면용 사람이 읽는 요약(`preview.summary`).

웹 앱은 위키 본문을 사용자에게 보이지 않는다. 승인 화면은 `changes`의 Markdown diff 대신 이 요약으로
어떤 개념 문장이 어떤 원문 인용으로 추가·삭제·수정되는지 보인다. 요약은 이미 `draft_hash`에 묶인
작업·제출·공개 상태에서 파생하므로 해시 입력에 넣지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

SUMMARY_KINDS = frozenset({
    "CONCEPT_CREATE", "SOURCE_PAGE", "STATEMENT_ADD", "STATEMENT_UPDATE", "STATEMENT_REMOVE",
    "EVIDENCE_INVALIDATION", "LINK_ADD", "LINK_REMOVE", "CONCEPT_MERGE", "CONCEPT_DELETE",
})

# 원문 변경으로 무효화된 문장 요약의 상한. 넘으면 상한까지만 싣고 `text`가 null인 EVIDENCE_INVALIDATION
# 항목 하나를 끝에 붙여 "더 있다"를 표시한다(그 항목의 페이지는 생략된 첫 문장의 페이지다).
MAX_INVALIDATION_ITEMS = 20

# 본문 표기용 근거 참조(`[doc_x:B0001]`). 문장 원문에는 보통 없지만 사람에게 보일 때는 빼 둔다.
_REFERENCE = re.compile(r"\s*\[[^\[\]\s]+:B\d+\]")


def plain_text(value: object) -> str:
    return _REFERENCE.sub("", str(value or "")).strip()


def summary_item(
    kind: str,
    page_id: str | None,
    page_title: str,
    *,
    text: object | None = None,
    before_text: object | None = None,
    related_page_title: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if kind not in SUMMARY_KINDS:
        raise ValueError(f"unknown summary kind: {kind}")
    return {
        "kind": kind,
        "page_id": page_id,
        "page_title": str(page_title),
        "text": None if text is None else plain_text(text),
        "before_text": None if before_text is None else plain_text(before_text),
        "related_page_title": related_page_title,
        "evidence": [
            {"snapshot_id": str(item["snapshot_id"]), "block_id": str(item["block_id"]), "quote": str(item["quote"])}
            for item in evidence or []
        ],
    }


def group_by_page(items: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """(페이지 키, 항목) 목록을 페이지가 처음 나온 순서로 묶는다. 페이지 안에서는 들어온(작업) 순서를 지킨다."""
    order: dict[str, int] = {}
    for key, _item in items:
        order.setdefault(key, len(order))
    return [item for _key, item in sorted(items, key=lambda pair: order[pair[0]])]


def cap_invalidations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) <= MAX_INVALIDATION_ITEMS:
        return items
    first_omitted = items[MAX_INVALIDATION_ITEMS]
    return [
        *items[:MAX_INVALIDATION_ITEMS],
        summary_item("EVIDENCE_INVALIDATION", first_omitted["page_id"], first_omitted["page_title"]),
    ]


def preview_item_count(preview: dict[str, Any]) -> int:
    """미리보기 상한(`MCP_WIKI_DRAFT_PREVIEW_MAX_ITEMS`)에 세는 항목 수. 요약도 센다."""
    return len(preview["changes"]) + len(preview.get("summary") or [])
