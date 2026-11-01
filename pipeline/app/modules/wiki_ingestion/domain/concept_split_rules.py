from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

# ADR-0042의 K·C·C_low·P는 초기값이다. 계획 0단계 측정으로 다시 정한다.

# 편입 승격: 세부 항목이 이만큼 배정된 항목만 개념 페이지가 된다. 0단계 측정 전 초기값.
MIN_PAGE_ITEMS = 3  # K
# lint 분리: 하위 항목의 자식이 이만큼이면 자식 페이지로 보낸다.
SPLIT_MIN_CHILDREN = 5  # C
# 근거 수는 기준이 아니다. 근거를 많이 붙이는 모델에서는 자식 없는 항목까지 분리돼 페이지가 다시 잘게 쪼개진다.
# 다시 합치기: 자식이 이만큼 이하로 줄어야 부모로 합친다.
# 분리 문턱(C)보다 낮아야 기준 근처에서 분리와 합치기를 반복하지 않는다.
MERGE_MAX_CHILDREN = 2  # C_low
# 여러 부모 승격: 참조가 이만큼 많은 부모 페이지에 걸리면 독립 개념으로 본다.
MIN_PROMOTION_REFERENCE_PAGES = 2
# 클러스터 승격: 같은 대분류 아래 하위 분류가 이만큼 모여야 한다.
MIN_CLUSTER_SUBCATEGORIES = 5  # P


@dataclass(frozen=True)
class EntryNode:
    """판정에 필요한 하위 항목(또는 분리된 자식 페이지) 요약."""

    entry_id: str
    child_entry_ids: tuple[str, ...]
    reference_page_ids: frozenset[str]
    is_child_page: bool = False


@dataclass(frozen=True)
class EntryDecision:
    action: Literal["split", "merge_back", "promote"]
    entry_id: str


def reaches_page_threshold(assigned_item_count: int) -> bool:
    return assigned_item_count >= MIN_PAGE_ITEMS


def cluster_reaches_promotion(subcategory_count: int) -> bool:
    return subcategory_count >= MIN_CLUSTER_SUBCATEGORIES


def decide_entry_changes(entries: Iterable[EntryNode]) -> list[EntryDecision]:
    """항목마다 결정은 하나다. 여러 부모 승격이 분리보다 먼저다. 결과는 entry_id 순."""
    decisions = []
    for entry in sorted(entries, key=lambda item: item.entry_id):
        action = _decide(entry)
        if action:
            decisions.append(EntryDecision(action=action, entry_id=entry.entry_id))
    return decisions


def _decide(entry: EntryNode) -> Literal["split", "merge_back", "promote"] | None:
    multi_parent = len(entry.reference_page_ids) >= MIN_PROMOTION_REFERENCE_PAGES
    if entry.is_child_page:
        # 되돌린 직후에도 따로 미루지 않는다. 되돌리기가 바꾼 페이지는 다음 lint가 바뀐 페이지로 본다.
        if multi_parent:
            return None
        if len(entry.child_entry_ids) <= MERGE_MAX_CHILDREN:
            return "merge_back"
        return None
    if multi_parent:
        return "promote"
    if len(entry.child_entry_ids) >= SPLIT_MIN_CHILDREN:
        return "split"
    return None
