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
    # 하위 항목 참조(entry_reference)는 같은 두 페이지 사이에도 항목마다 따로 남긴다.
    # DB 간선과 고아 판정은 페이지 단위(_link_key)라, 참조가 하나라도 남으면 간선이 남는다.
    supported: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    # lint가 옮긴 관계 링크는 원래 링크를 낸 기록(`requires_operation_id`)이 남아 있을 때만 잇는다(ADR 0042 결정 6).
    present = {str(artifact.get("operation_id")) for artifact in operation_artifacts}
    for artifact in operation_artifacts:
        for link in artifact.get("removed_links", []):
            if not isinstance(link, dict):
                continue
            if link.get("entry_id"):
                supported.pop(_replay_key(link), None)
            else:
                # 수동 편집처럼 entry_id 없이 지운 기록은 그 페이지 쌍·relation의 참조를 모두 지운다.
                for key in [key for key in supported if key[:3] == _link_key(link)]:
                    del supported[key]
        added = (
            artifact.get("added_links", [])
            if artifact.get("artifact_type") in ("lint", "manual")
            else artifact.get("links", [])
        )
        for link in added:
            if isinstance(link, dict) and str(link.get("requires_operation_id") or artifact.get("operation_id")) in present:
                supported[_replay_key(link)] = link
    return list(supported.values())


def _replay_key(link: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (*_link_key(link), str(link.get("entry_id") or ""), str(link.get("parent_entry_id") or ""))


def _link_key(link: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(link.get("source") or ""),
        str(link.get("target") or ""),
        str(link.get("relation") or "related_to"),
    )
