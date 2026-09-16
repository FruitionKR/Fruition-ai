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
    return {
        "removed_links": [
            link for link in added_links if _link_key(link) not in supported_keys
        ],
        "restored_links": [
            link for link in removed_links if _link_key(link) in supported_keys
        ],
    }


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
