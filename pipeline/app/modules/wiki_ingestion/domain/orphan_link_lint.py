from __future__ import annotations

from typing import Any


def find_orphan_links(
    *,
    current_links: list[dict[str, Any]],
    active_contribution_json: list[dict[str, Any]],
    managed_contribution_json: list[dict[str, Any]],
    deleted_page_refs: set[str],
) -> list[dict[str, Any]]:
    active_link_keys = {
        _link_key(link)
        for link in replay_supported_links(active_contribution_json)
    }
    managed_link_keys = {
        _link_key(link)
        for contribution in managed_contribution_json
        for field in ("links", "added_links", "removed_links")
        for link in contribution.get(field, [])
        if isinstance(link, dict)
    }
    orphan_links: list[dict[str, Any]] = []
    for link in current_links:
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if source in deleted_page_refs or target in deleted_page_refs:
            reason = "endpoint_deleted"
        elif (
            _link_key(link) in managed_link_keys
            and _link_key(link) not in active_link_keys
        ):
            reason = "no_active_support"
        else:
            continue
        orphan_links.append({**link, "reason": reason})
    return orphan_links


def replay_supported_links(
    operation_artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    supported: dict[tuple[str, str, str], dict[str, Any]] = {}
    for artifact in operation_artifacts:
        for link in artifact.get("removed_links", []):
            if isinstance(link, dict):
                supported.pop(_link_key(link), None)
        added = (
            artifact.get("added_links", [])
            if artifact.get("artifact_type") == "lint"
            else artifact.get("links", [])
        )
        for link in added:
            if isinstance(link, dict):
                supported[_link_key(link)] = link
    return list(supported.values())


def _link_key(link: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(link.get("source") or ""),
        str(link.get("target") or ""),
        str(link.get("relation") or "related_to"),
    )
