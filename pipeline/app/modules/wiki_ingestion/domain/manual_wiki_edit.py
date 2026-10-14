from __future__ import annotations

import re
from typing import Any


_WIKILINK = re.compile(r"\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]")


def wikilink_slugs(markdown: str) -> list[str]:
    """본문의 `[[slug]]`·`[[slug|표시 이름]]`에서 slug를 등장 순서대로 중복 없이 꺼낸다."""
    return list(dict.fromkeys(
        match.group(1).strip()
        for match in _WIKILINK.finditer(markdown)
        if match.group(1).strip()
    ))


def manual_link_changes(
    *,
    page_ref: str,
    old_markdown: str,
    new_markdown: str,
    current_links: list[dict[str, Any]],
    target_refs: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """사람이 고친 본문의 `[[slug]]` 차이를 edge 추가·삭제로 바꾼다.

    본문에서 바뀐 slug만 반영한다. 손대지 않은 링크와, 본문에 없던 기존 edge는 그대로 둔다.
    지운 slug는 그 대상으로 가는 edge를 종류와 무관하게 모두 지우고, 새 slug는 활성 페이지가
    있을 때만 edge를 만든다. `target_refs`는 새 본문의 slug를 `page_type:slug`로 푼 값이다.
    """
    old_slugs = set(wikilink_slugs(old_markdown))
    new_slugs = wikilink_slugs(new_markdown)
    dropped = old_slugs - set(new_slugs)
    removed = [
        {"source": page_ref, "target": str(link["target"]), "relation": str(link["relation"])}
        for link in current_links
        if str(link["target"]).split(":", 1)[-1] in dropped
    ]
    linked = {str(link["target"]) for link in current_links}
    added = []
    for slug in new_slugs:
        target = target_refs.get(slug)
        if slug in old_slugs or target is None or target == page_ref or target in linked:
            continue
        relation = (
            "source_mentions_concept"
            if page_ref.startswith("source:") and target.startswith("concept:")
            else "related_to"
        )
        added.append({"source": page_ref, "target": target, "relation": relation})
    return added, removed
