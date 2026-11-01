from __future__ import annotations

from typing import Any


def calculate_lint_link_changes(
    target_contributions: list[dict[str, Any]],
    supported_links: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    supported_keys = {_link_key(link) for link in supported_links}
    added_links = _unique_links(
        link
        for contribution in target_contributions
        for link in contribution.get("added_links", [])
        if isinstance(link, dict)
    )
    removed_links = _unique_links(
        link
        for contribution in target_contributions
        for link in contribution.get("removed_links", [])
        if isinstance(link, dict)
    )
    changes = {
        "removed_links": [
            link for link in added_links if _link_key(link) not in supported_keys
        ],
        "restored_links": [
            link for link in removed_links if _link_key(link) in supported_keys
        ],
    }
    # 분리 롤백은 자식을 부모로 합치는 것과 같다(ADR 0042 6). 분리 기록(split_from)은 그 lint가 만든
    # 자식 페이지에만 있고, 만든 페이지는 롤백에서 지워지므로 자식 → 부모 방향 전환을 남긴다.
    redirects = [
        {"from_page_id": contribution["page_id"], "to_page_id": contribution["split_from"]["page_id"]}
        for contribution in target_contributions
        if isinstance(contribution.get("split_from"), dict)
    ]
    if redirects:
        changes["redirects"] = redirects
    # 다시 합치기 롤백: 지운 자식의 합친 기록(merged_into)에 남긴 간선을 자식으로 되돌린다. 자식은 재조립으로 되살아난다.
    reversed_redirects = [
        {
            "page_id": contribution["page_id"],
            "merged_into_page_id": contribution["merged_into"]["page_id"],
            "links": contribution.get("redirected_links", []),
            "pages": contribution.get("redirected_pages", []),
        }
        for contribution in target_contributions
        if isinstance(contribution.get("merged_into"), dict)
    ]
    if reversed_redirects:
        changes["reversed_redirects"] = reversed_redirects
    return changes


def _unique_links(values: Any) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for value in values:
        by_key.setdefault(_link_key(value), dict(value))
    return list(by_key.values())


def _link_key(link: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(link.get("source") or ""),
        str(link.get("target") or ""),
        str(link.get("relation") or "related_to"),
    )
