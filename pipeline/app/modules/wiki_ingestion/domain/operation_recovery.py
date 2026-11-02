from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


PageType = Literal["source", "concept"]
RecoveryAction = Literal["unchanged", "restore", "rebuild", "delete"]


class PageRebuildError(ValueError):
    pass


class RestoreConflictError(ValueError):
    """복원 재생이 뒤 작업과 충돌한다(같은 항목이 두 페이지에 재생되는 등). 현재 상태를 쓰기 전에 거절한다(ADR 0042)."""

    code = "restore_conflict"


@dataclass(frozen=True)
class PageContribution:
    page_id: str
    page_type: PageType
    operation_id: str
    sequence: int
    markdown_key: str
    contribution: dict[str, Any] | None = None
    active: bool = True


@dataclass(frozen=True)
class PageRecoveryPlan:
    page_id: str
    page_type: PageType
    action: RecoveryAction
    kept_contributions: tuple[PageContribution, ...]
    removed_contributions: tuple[PageContribution, ...]


def plan_page_recovery(
    contributions: list[PageContribution],
    excluded_operations: set[str],
) -> PageRecoveryPlan:
    if not contributions:
        raise ValueError("page recovery requires at least one contribution")

    ordered = sorted(
        (item for item in contributions if item.active),
        key=lambda item: item.sequence,
    )
    if not ordered:
        raise ValueError("page recovery requires at least one active contribution")

    page_id = ordered[0].page_id
    page_type = ordered[0].page_type
    if any(
        item.page_id != page_id or item.page_type != page_type
        for item in ordered
    ):
        raise ValueError("page recovery contributions must belong to one page")

    removed = tuple(
        item for item in ordered if item.operation_id in excluded_operations
    )
    kept = tuple(
        item for item in ordered if item.operation_id not in excluded_operations
    )
    if not removed:
        action: RecoveryAction = "unchanged"
    elif not kept:
        action = "delete"
    elif page_type == "source":
        action = "restore" if ordered[-1] in removed else "unchanged"
    elif kept[-1].sequence < removed[0].sequence:
        action = "restore"
    else:
        action = "rebuild"

    return PageRecoveryPlan(
        page_id=page_id,
        page_type=page_type,
        action=action,
        kept_contributions=kept,
        removed_contributions=removed,
    )
