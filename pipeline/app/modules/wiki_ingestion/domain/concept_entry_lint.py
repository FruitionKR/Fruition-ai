"""lint 하위 항목 재검사(ADR 0042 3단계).

바뀐 페이지의 하위 항목으로 분리·여러 부모 승격·다시 합치기를 정하고, 그대로 재생할 기여 기록을 만든다.
- 분리·승격: 집 페이지에는 옮긴 항목마다 `remove`와 새 페이지를 가리키는 `entry_reference`를 남긴다. 새 페이지에는
  개념과 `split_from`을 남기고, 옮긴 항목의 원래 기록(편입, 클러스터 승격·병합 lint)을 원래 작업 id 그대로 잇는다(`linked`).
- 다시 합치기: 부모에 자식 항목마다 `revive`를 남겨 분리 때 지운 항목을 되살리고, 분리 뒤 자식에만 쌓인 편입 기록을
  부모에 잇는다. 자식에는 재생 가능한 삭제 기록을 남긴다. 방향 전환은 두지 않는다.
- 다른 페이지의 참조는 옮긴 항목을 따라 대상을 바꾸고, 가리키던 항목이 없어진 참조는 지운다.
- 페이지 이동: 홀로 선 페이지를 판정기가 고른 다른 페이지(그 페이지나 그 항목 아래)의 하위 항목으로 옮긴다. 다시 합치기와
  같은 기록이다. 대상 페이지에는 옮긴 페이지의 원래 편입 기록을 원래 작업 id로 잇고(항목 `add`와 근거), 옮긴 페이지에는
  재생 가능한 삭제 기록을 남긴다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.modules.wiki_ingestion.domain.concept_entries import (
    ENTRY_EVIDENCE_FIELDS,
    ENTRY_REFERENCE,
    _ancestors,
    new_entry_id,
)
from app.modules.wiki_ingestion.domain.concept_split_rules import (
    EntryNode,
    decide_entry_changes,
)

# 편입이 아닌 기록. 수동 기록은 옮길 수 없고, lint 기록은 클러스터 승격·병합의 `add`만 옮길 근거를 가진다.
_LINT = "lint"
_MANUAL = "manual"
CONCEPT_RELATION = "concept_related_to"


@dataclass(frozen=True)
class EntryPage:
    page_id: str
    slug: str
    title: str
    # 적용 순서의 원본 기여 JSON(전역화 전). 원래 작업 id로 다른 페이지에 이을 때 그대로 복사한다.
    contributions: tuple[dict[str, Any], ...]
    entries: tuple[dict[str, Any], ...]
    links: tuple[dict[str, Any], ...]

    @property
    def ref(self) -> str:
        return f"concept:{self.slug}"

    @property
    def split_record(self) -> dict[str, Any] | None:
        """이 페이지를 만든 분리 기록. 분리로 만든 자식 페이지만 가진다.

        재생처럼 마지막 삭제 기록 뒤만 본다. 합쳐 지운 뒤 같은 slug로 다시 만든 페이지는 자식 페이지가 아니다.
        """
        return next(
            (item for item in reversed(_since_last_delete(self.contributions)) if isinstance(item.get("split_from"), dict)),
            None,
        )


@dataclass
class EntryLintPlan:
    decisions: list[dict[str, Any]] = field(default_factory=list)
    # page_id → 이번 lint 기록에 넣을 필드(slug·title·content_action·entry_changes·added_links·removed_links 등)
    page_changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 원래 작업 id 그대로 다른 페이지에 잇는 기여 JSON
    linked: list[dict[str, Any]] = field(default_factory=list)
    # 다시 합쳐 지울 자식 page_id
    deleted_page_ids: list[str] = field(default_factory=list)
    # 이번에 자식을 합쳐 받아 분리·승격을 미룬 부모 페이지. 다음 lint가 다시 본다.
    deferred_page_ids: list[str] = field(default_factory=list)

    def link(self, record: dict[str, Any]) -> None:
        """기록은 페이지·작업마다 하나다. 같은 작업을 같은 페이지에 여러 번 이으면 한 기록으로 합친다."""
        same = next(
            (item for item in self.linked
             if item["page_id"] == record["page_id"] and item["operation_id"] == record["operation_id"]),
            None,
        )
        if same is None:
            self.linked.append(record)
            return
        same["entry_changes"].extend(record["entry_changes"])
        for field in ("links", "added_links"):
            same.setdefault(field, []).extend(link for link in record.get(field, []) if link not in same[field])
        known = {unit.get("evidence_id") for unit in same["evidence_units"]}
        same["evidence_units"].extend(unit for unit in record["evidence_units"] if unit.get("evidence_id") not in known)

    def change(self, page_id: str, slug: str, title: str) -> dict[str, Any]:
        return self.page_changes.setdefault(
            page_id,
            {
                "page_id": page_id,
                "slug": slug,
                "title": title,
                "content_action": "none",
                "entry_changes": [],
                "added_links": [],
                "removed_links": [],
            },
        )


def plan_entry_lint(
    pages: dict[str, EntryPage],
    changed_page_ids: set[str],
    new_page: Callable[[EntryPage, dict[str, Any]], tuple[str, str] | None],
    deleted_refs: frozenset[str] = frozenset(),
) -> EntryLintPlan:
    """pages는 바뀐 페이지와 참조로 이어진 이웃 페이지다. 판정은 바뀐 페이지만 한다.

    new_page(집 페이지, 항목) → (page_id, slug). 쓸 수 있는 slug가 없으면 None이다.
    deleted_refs는 참조가 가리키지만 지워진 페이지 ref다. 그 참조도 사라진 참조로 지운다.
    """
    plan = EntryLintPlan()
    nodes: list[EntryNode] = []
    targets: dict[str, tuple[EntryPage, dict[str, Any] | None]] = {}
    for page_id in sorted(changed_page_ids):
        page = pages.get(page_id)
        if page is None:
            continue
        record = page.split_record
        root = record["split_from"]["entry_id"] if record else None
        if record:
            node_id = f"page:{page.page_id}"
            nodes.append(
                EntryNode(
                    entry_id=node_id,
                    # 항목 근거와, 분리 뒤 자식 페이지 자체에 쌓인 근거(항목 변경 없는 편입 기록)를 함께 센다.
                    evidence_count=_evidence_count(page.entries) + sum(
                        len(item.get("evidence_units", []))
                        for item in page.contributions
                        if item.get("artifact_type") not in (_LINT, _MANUAL) and not item.get("entry_changes")
                    ),
                    child_entry_ids=tuple(item["entry_id"] for item in page.entries if item["entry_id"] != root),
                    reference_page_ids=_referencing_pages(pages, page, {item["entry_id"] for item in page.entries})
                    - {record["split_from"]["page_id"]},
                    is_child_page=True,
                )
            )
            targets[node_id] = (page, None)
        for entry in page.entries:
            # 자식 페이지의 뿌리 항목은 페이지 자신이라 다시 분리하지 않는다.
            if entry["entry_id"] == root:
                continue
            nodes.append(
                EntryNode(
                    entry_id=entry["entry_id"],
                    evidence_count=_evidence_count(_subtree(page.entries, entry["entry_id"])),
                    child_entry_ids=tuple(
                        item["entry_id"] for item in page.entries if item["parent_entry_id"] == entry["entry_id"]
                    ),
                    reference_page_ids=_referencing_pages(pages, page, {entry["entry_id"]}),
                )
            )
            targets[entry["entry_id"]] = (page, entry)

    moved: set[str] = set()
    merged_parents: set[str] = set()
    # 옮기기로 한 항목을 먼저 모두 정하고 기록은 그 뒤에 만든다. 같은 lint에서 함께 옮긴 항목끼리의 참조도
    # 최종 위치로 잇기 위해서다. location: 항목 id → (원래 집 ref, 새 페이지 ref).
    moves: list[tuple[EntryPage, dict[str, Any], str, str]] = []
    location: dict[str, tuple[str, str]] = {}
    depth = {
        entry["entry_id"]: len(_ancestors({item["entry_id"]: item for item in page.entries}, entry["entry_id"]))
        for page, entry in targets.values()
        if entry is not None
    }
    # 합치기를 먼저 한다. 합쳐 없어질 페이지의 항목은 분리·승격하지 않는다. 분리·승격은 위 항목부터 해서
    # 이미 옮긴 항목의 아래 항목을 다시 옮기지 않는다(id 순서가 계층과 달라도).
    decisions = sorted(
        decide_entry_changes(nodes),
        key=lambda item: (item.action != "merge_back", depth.get(item.entry_id, 0), item.entry_id),
    )
    for decision in decisions:
        page, entry = targets[decision.entry_id]
        if entry is None:
            reason = _merge_back(plan, pages, page)
            if reason is None:
                merged_parents.add(str(page.split_record["split_from"]["page_id"]))
        elif page.page_id in plan.deleted_page_ids:
            reason = "page_merged"
        elif page.page_id in merged_parents:
            # 이번에 자식을 합쳐 받은 부모는 합친 뒤 항목으로 다음 lint가 나눈다. 합치기 전 항목으로 나누면
            # 되살린 항목이 지워지기만 하고 새 페이지로 이어지지 않는다.
            reason = "parent_merged"
            if page.page_id not in plan.deferred_page_ids:
                plan.deferred_page_ids.append(page.page_id)
        elif moved & _ancestors({item["entry_id"]: item for item in page.entries}, entry["entry_id"]):
            reason = "ancestor_moved"
        else:
            created = new_page(page, entry)
            reason = None if created else "slug_taken"
            if created:
                moves.append((page, entry, *created))
                for item in _subtree(page.entries, entry["entry_id"]):
                    moved.add(item["entry_id"])
                    location[item["entry_id"]] = (page.ref, f"concept:{created[1]}")
        plan.decisions.append(
            {
                "action": decision.action,
                "page_id": page.page_id,
                "entry_id": entry["entry_id"] if entry else None,
                **({"skipped_reason": reason} if reason else {}),
            }
        )
    for home, entry, page_id, slug in moves:
        _move_to_new_page(plan, home, entry, page_id, slug)
    _relocate_references(plan, pages, location)
    _drop_dangling_references(plan, pages, deleted_refs)
    return plan


def _move_to_new_page(
    plan: EntryLintPlan,
    home: EntryPage,
    entry: dict[str, Any],
    page_id: str,
    slug: str,
) -> None:
    subtree = _subtree(home.entries, entry["entry_id"])
    moved_ids = {item["entry_id"] for item in subtree}
    home_change = plan.change(home.page_id, home.slug, home.title)
    # 옮긴 항목을 하나씩 지운다. 뿌리 항목을 만든 편입을 되돌려도 다른 편입이 더한 아래 항목이 집에 남지 않는다.
    home_change["entry_changes"].extend({"op": "remove", "entry_id": item["entry_id"]} for item in subtree)
    # 집에는 옮긴 자리에 새 페이지로 가는 요약 줄(참조)을 남긴다.
    home_change["added_links"].append(
        {
            "source": home.ref,
            "target": f"concept:{slug}",
            "relation": ENTRY_REFERENCE,
            "entry_id": entry["entry_id"],
            "name": entry["name"],
            "parent_entry_id": entry["parent_entry_id"],
        }
    )
    linked_operation_ids = []
    for contribution in home.contributions:
        changes = [
            change
            for change in contribution.get("entry_changes", [])
            if isinstance(change, dict) and change.get("entry_id") in moved_ids
        ]
        if contribution.get("artifact_type") == _LINT:
            # 클러스터 승격·병합 lint 기록이 더한 항목은 그 기록이 원래 기록이다(ADR 0042 4단계). 옮긴 항목의 `add`만 잇는다.
            changes = [change for change in changes if change.get("op") == "add"]
        if changes and contribution.get("artifact_type") != _MANUAL:
            plan.link(_linked_copy(contribution, page_id, slug, changes))
            linked_operation_ids.append(str(contribution["operation_id"]))
    child = plan.change(page_id, slug, entry["name"])
    child.update(
        content_action="create",
        definition=f"{home.title}에서 나눈 하위 항목이다.",
        split_from={"page_id": home.page_id, "slug": home.slug, "entry_id": entry["entry_id"]},
        linked_operation_ids=linked_operation_ids,
    )



def _relocate_references(
    plan: EntryLintPlan,
    pages: dict[str, EntryPage],
    location: dict[str, tuple[str, str]],
) -> None:
    """참조의 출발(아래 둔 항목)과 대상 항목을 이번 lint로 옮긴 최종 위치로 바꾼다.

    옮긴 항목 아래 둔 참조는 새 페이지에서 나가고, 옮긴 항목을 가리키는 참조는 새 페이지를 가리킨다. 둘 다 이번에
    옮겼으면 새 페이지끼리 잇는다.
    """
    new_pages = {ref: page_id for page_id, change in plan.page_changes.items() for ref in [f"concept:{change['slug']}"]}
    for page in pages.values():
        for link in page.links:
            if link.get("relation") != ENTRY_REFERENCE:
                continue
            moved_source = location.get(str(link.get("parent_entry_id")))
            source = moved_source[1] if moved_source and moved_source[0] == page.ref else str(link.get("source"))
            moved_target = location.get(str(link.get("entry_id")))
            target = moved_target[1] if moved_target and moved_target[0] == link.get("target") else str(link.get("target"))
            if (source, target) == (link.get("source"), link.get("target")):
                continue
            change = plan.change(page.page_id, page.slug, page.title)
            if link in change["removed_links"]:
                continue
            change["removed_links"].append(link)
            owner = plan.page_changes[new_pages[source]] if source != page.ref else change
            owner["added_links"].append({**link, "source": source, "target": target})


def _merge_back(plan: EntryLintPlan, pages: dict[str, EntryPage], child: EntryPage) -> str | None:
    record = child.split_record or {}
    split_from = record.get("split_from") or {}
    parent = pages.get(str(split_from.get("page_id")))
    if parent is None or parent.page_id in plan.deleted_page_ids:
        return "parent_missing"
    # ponytail: 자식이 다른 페이지 항목을 참조하면(다시 나눈 손자 페이지 포함) 합치지 않는다.
    # 참조를 부모로 옮기고 손자의 부모를 바꾸는 처리는 필요해지면 더한다.
    if any(link.get("relation") == ENTRY_REFERENCE for link in child.links):
        return "has_references"
    linked = set(record.get("linked_operation_ids", []))
    parent_operations = {str(item.get("operation_id")) for item in parent.contributions}
    copies = []
    # 재생처럼 마지막 삭제 기록 뒤 기록만 옮긴다.
    for contribution in _since_last_delete(child.contributions):
        operation_id = str(contribution.get("operation_id"))
        changes = [change for change in contribution.get("entry_changes", []) if isinstance(change, dict)]
        # 사람이 고친 자식 본문은 부모로 옮길 수 없다.
        if contribution.get("artifact_type") == _MANUAL:
            return "manual_edit"
        if contribution.get("artifact_type") == _LINT or operation_id in linked:
            continue
        # ponytail: 기록은 페이지·작업마다 하나라, 분리 뒤 같은 작업이 두 페이지에 다 기여했으면 잇지 못해 합치지 않는다.
        if operation_id in parent_operations:
            return "operation_on_both_pages"
        # 분리 뒤 자식에 쌓인 기록은 항목 변경이 없어도(페이지 근거만 있어도) 원래 작업 id로 부모에 잇는다.
        copies.append(_linked_copy(contribution, parent.page_id, parent.slug, changes, all_evidence=True))
    for copy in copies:
        plan.link(copy)
    change = plan.change(parent.page_id, parent.slug, parent.title)
    # 분리 때 지운 항목을 되살린다. 뿌리 항목이 빠졌어도 남은 아래 항목은 각자 되살린다.
    change["entry_changes"].extend(
        {"op": "revive", "entry_id": entry_id}
        for entry_id in dict.fromkeys([split_from["entry_id"], *(item["entry_id"] for item in child.entries)])
    )
    change["removed_links"].extend(
        link for link in parent.links if link.get("relation") == ENTRY_REFERENCE and link.get("target") == child.ref
    )
    _retarget_references(
        plan,
        pages,
        child,
        {item["entry_id"] for item in child.entries},
        parent.ref,
        {parent.page_id, child.page_id},
    )
    _delete_page(plan, child)
    return None


def _delete_page(plan: EntryLintPlan, page: EntryPage) -> None:
    """옮겨 없어질 페이지에 재생 가능한 삭제 기록을 남긴다(다시 합치기·페이지 이동).

    기존 기여는 그대로 두고, 이 기록이 그때까지의 기록을 지운다. lint 복원이 이 기록과 이은 기록을 빼면 페이지가 다시
    재생된다. 페이지가 내던 링크는 이 기록이 지운 것으로 남겨 복원 때 되살린다. 다른 페이지에서 이 페이지로 오던 일반
    링크는 저장할 때 그 페이지 기록에서 지운다.
    """
    change = plan.change(page.page_id, page.slug, page.title)
    change["content_action"] = "delete"
    change["removed_links"].extend(link for link in page.links if link not in change["removed_links"])
    plan.deleted_page_ids.append(page.page_id)


def standalone(page: EntryPage) -> bool:
    """다른 페이지의 하위 항목으로 옮길 수 있는 홀로 선 페이지인지.

    하위 항목·분리 기록·사람이 고친 기록·다른 페이지 항목 참조가 없고, 근거를 가진 lint 기록(클러스터 승격·병합)도 없다.
    lint 기록은 원래 작업 id로 잇지 않으므로 근거가 있으면 옮길 때 잃는다.
    """
    live = _since_last_delete(page.contributions)
    return not (
        page.entries
        or page.split_record
        or any(link.get("relation") == ENTRY_REFERENCE for link in page.links)
        or any(
            item.get("artifact_type") == _MANUAL
            or (item.get("artifact_type") == _LINT and item.get("evidence_units"))
            for item in live
        )
    )


def _since_last_delete(contributions: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """재생에 남는 기록. 재생은 마지막 삭제 기록(lint `content_action: "delete"`)과 그 앞 기록을 빼고 한다."""
    last = max(
        (index for index, item in enumerate(contributions)
         if item.get("artifact_type") == _LINT and item.get("content_action") == "delete"),
        default=-1,
    )
    return contributions[last + 1:]


def choose_page_moves(
    ranked: list[tuple[float, str, str]],
    head_pages: dict[str, tuple[str, str | None]],
    busy: set[str],
) -> tuple[list[tuple[str, str, str | None]], list[tuple[str, str]]]:
    """part_of 판정(확률, 상위, 옮길 page_id, 확률 순)에서 이번 lint에 옮길 페이지를 고른다.

    페이지마다 확률이 가장 높은 상위 하나만 본다. head_pages는 상위 → (그 상위가 있는 page_id, 상위 항목 id 또는 None),
    busy는 이번 lint의 다른 판정(분리·승격·합치기·참조 정리)이 바꾸는 페이지다. 옮길 페이지나 상위 페이지가 busy면
    `page_busy`, 이번에 옮긴 페이지를 상위로 두거나 이번에 상위가 된 페이지를 옮기면 `chained`로 다음 lint에 미룬다.
    결과는 ([(page_id, 대상 page_id, 상위 항목 id)], [(page_id, 미룬 이유)])다.
    """
    moves: list[tuple[str, str, str | None]] = []
    deferred: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, head, page_id in ranked:
        if page_id in seen:
            continue
        seen.add(page_id)
        target, parent_entry_id = head_pages[head]
        if page_id in busy or target in busy:
            deferred.append((page_id, "page_busy"))
        elif any(target == moved for moved, _, _ in moves) or any(page_id == item[1] for item in moves):
            deferred.append((page_id, "chained"))
        else:
            moves.append((page_id, target, parent_entry_id))
    return moves, deferred


def move_page_under(
    plan: EntryLintPlan,
    page: EntryPage,
    definition: str,
    target: EntryPage,
    parent_entry_id: str | None,
) -> str | None:
    """홀로 선 페이지를 target의 하위 항목 하나로 옮긴다. 옮기지 못하면 이유를 돌려준다.

    페이지의 편입 기록마다 같은 작업 id로 target에 기록을 잇는다. 그 기록은 페이지 이름·정의로 한 항목을 `add`하고
    (근거는 그 기록의 개념 인용과 근거 단위) 근거 단위를 모두 옮긴다. 그래서 그 편입을 되돌리면 target에서도 그 근거가
    빠지고, 모두 되돌리면 항목이 사라진다. 기록은 페이지·작업마다 하나라 같은 작업이 target에도 기여했으면 옮기지 않는다.
    """
    target_operations = {str(item.get("operation_id")) for item in target.contributions}
    entry_id = new_entry_id(target.slug, page.slug)
    copies = []
    # 지웠다가 다시 편입한 페이지면 삭제 기록 앞 기록은 이미 다른 페이지로 옮겼거나 지운 것이라 잇지 않는다.
    for contribution in _since_last_delete(page.contributions):
        if contribution.get("artifact_type") == _LINT:
            continue
        if str(contribution.get("operation_id")) in target_operations:
            return "operation_on_both_pages"
        units = [unit for unit in contribution.get("evidence_units", []) if isinstance(unit, dict)]
        add = {
            "op": "add",
            "entry_id": entry_id,
            "name": page.title,
            "definition": definition,
            "parent_entry_id": parent_entry_id,
            "anchor_reference_ids": list(dict.fromkeys([
                *(contribution.get("concept") or {}).get("anchor_reference_ids", []),
                *(ref for unit in units for ref in unit.get("anchor_reference_ids", [])),
            ])),
            "evidence_claim_ids": [str(unit["evidence_id"]) for unit in units if unit.get("evidence_id")],
        }
        copies.append(_linked_copy(contribution, target.page_id, target.slug, [add], all_evidence=True))
    for copy in copies:
        plan.link(copy)
    # 대상 페이지에도 이 lint 기록을 둔다. 이은 기록만 있으면 document-svc가 그 페이지 행을 받지 않는다.
    # 기록에는 이은 작업을 남긴다(재생에는 쓰지 않는다).
    linked = plan.change(target.page_id, target.slug, target.title).setdefault("linked_operation_ids", [])
    linked.extend(str(copy["operation_id"]) for copy in copies if str(copy["operation_id"]) not in linked)
    _delete_page(plan, page)
    return None


def _linked_copy(
    contribution: dict[str, Any],
    page_id: str,
    slug: str,
    changes: list[dict[str, Any]],
    *,
    all_evidence: bool = False,
) -> dict[str, Any]:
    """옮긴 항목의 원래 편입 기록을 같은 작업 id로 다른 페이지에 잇는다. 개념 없이 항목과 그 근거만 가진다.

    all_evidence면 항목에 딸리지 않은 페이지 근거까지 모두 옮긴다(다시 합치기).
    """
    claim_ids = {claim_id for change in changes for claim_id in change.get("evidence_claim_ids", [])}
    return {
        **{key: contribution[key] for key in ("schema_version", "artifact_type", "operation_id", "document_id") if key in contribution},
        "page_id": page_id,
        "entry_changes": list(changes),
        "evidence_units": [
            {**unit, "related_concept_slugs": [slug]}
            for unit in contribution.get("evidence_units", [])
            if isinstance(unit, dict) and (all_evidence or unit.get("evidence_id") in claim_ids)
        ],
        "source_blocks": [],
        "links": [],
    }


def _retarget_references(
    plan: EntryLintPlan,
    pages: dict[str, EntryPage],
    home: EntryPage,
    entry_ids: set[str],
    target: str,
    skip_page_ids: set[str],
) -> None:
    for page in pages.values():
        if page.page_id in skip_page_ids:
            continue
        for link in page.links:
            if _references(link, home.ref, entry_ids):
                change = plan.change(page.page_id, page.slug, page.title)
                change["removed_links"].append(link)
                change["added_links"].append({**link, "target": target})


def _drop_dangling_references(plan: EntryLintPlan, pages: dict[str, EntryPage], deleted_refs: frozenset[str]) -> None:
    """가리키던 항목이나 집 페이지가 없어진 참조를 지운다. 참조 사본·본문 목록·간선은 이 기록의 재생으로 함께 정리된다."""
    homes = {page.ref: page for page in pages.values()}
    for page in pages.values():
        for link in page.links:
            if link.get("relation") != ENTRY_REFERENCE:
                continue
            home = homes.get(str(link.get("target")))
            if home is None and link.get("target") not in deleted_refs:
                continue
            if home is not None and link.get("entry_id") in {item["entry_id"] for item in home.entries}:
                continue
            change = plan.change(page.page_id, page.slug, page.title)
            if link not in change["removed_links"]:
                change["removed_links"].append(link)


def _references(link: dict[str, Any], target: str, entry_ids: set[str]) -> bool:
    return link.get("relation") == ENTRY_REFERENCE and link.get("target") == target and link.get("entry_id") in entry_ids


def _referencing_pages(pages: dict[str, EntryPage], home: EntryPage, entry_ids: set[str]) -> frozenset[str]:
    return frozenset(
        page.page_id
        for page in pages.values()
        if page.page_id != home.page_id and any(_references(link, home.ref, entry_ids) for link in page.links)
    )


def _subtree(entries: tuple[dict[str, Any], ...], root: str) -> list[dict[str, Any]]:
    by_id = {item["entry_id"]: item for item in entries}
    return [item for item in entries if root in _ancestors(by_id, item["entry_id"])]


def _evidence_count(entries: Any) -> int:
    return len({value for entry in entries for key in ENTRY_EVIDENCE_FIELDS for value in entry.get(key, [])})


def retarget_split_relations(
    plan: EntryLintPlan,
    pages: dict[str, EntryPage],
    related: Callable[[EntryPage, list[str]], set[str] | None],
) -> None:
    """분리로 나간 자식 D와 남은 부모 B 중 어느 쪽과 관련인지 다시 정해 B의 개념 관계를 옮긴다(ADR 0042 결정 6).

    B의 `concept_related_to`(나가는·들어오는 쪽 모두)마다 상대 개념 A를 묻는다. related(A, [B ref, D ref...])는 관련인
    후보 ref 집합이고, 판정할 수 없으면 None이다. D 쪽만이면 A–B를 A–D로 옮기고, B와 D 모두면 A–D를 더하고, B 쪽이거나
    판정할 수 없으면 그대로 둔다. 옮긴 링크는 원래 링크를 낸 기록에 종속된다. B가 내던 링크는 그 기록을 원래 작업 id로
    D에 잇고(`linked`), A가 내던 링크는 A의 lint 기록에 `requires_operation_id`로 둔다. 그래서 원래 링크를 만든 작업을
    되돌리면 옮긴 링크도 사라지고, 이 lint를 되돌리면 원래 링크가 돌아온다.
    """
    children: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
    for page_id, change in plan.page_changes.items():
        if isinstance(change.get("split_from"), dict):
            children.setdefault(str(change["split_from"]["page_id"]), {})[f"concept:{change['slug']}"] = (page_id, change)
    by_ref = {page.ref: page for page in pages.values()}
    for home_id, child_refs in sorted(children.items()):
        home = pages.get(home_id)
        if home is None:
            continue
        for emitter in list(pages.values()):
            for link in emitter.links:
                if link.get("relation") != CONCEPT_RELATION:
                    continue
                outgoing = emitter.page_id == home_id
                if not outgoing and link.get("target") != home.ref:
                    continue
                other = by_ref.get(str(link.get("target"))) if outgoing else emitter
                if other is None or other.ref in child_refs:
                    continue
                found = related(other, [home.ref, *child_refs])
                if not found or not found & set(child_refs):
                    continue
                supporting = [
                    item for item in emitter.contributions
                    if _link_key(link) in {
                        _link_key(record)
                        for record in item.get("added_links" if item.get("artifact_type") in (_LINT, _MANUAL) else "links", [])
                    }
                ]
                if not supporting:
                    continue
                for child_ref in sorted(found & set(child_refs)):
                    child_id, child_change = child_refs[child_ref]
                    if outgoing:
                        for item in supporting:
                            if item.get("artifact_type") == _MANUAL:
                                continue
                            copy = _linked_copy(item, child_id, child_change["slug"], [])
                            copy["added_links" if item.get("artifact_type") == _LINT else "links"] = [
                                {**link, "source": child_ref}
                            ]
                            plan.link(copy)
                            if str(item["operation_id"]) not in child_change["linked_operation_ids"]:
                                child_change["linked_operation_ids"].append(str(item["operation_id"]))
                    else:
                        change = plan.change(emitter.page_id, emitter.slug, emitter.title)
                        change["added_links"].extend(
                            {**link, "target": child_ref,
                             "requires_operation_id": str(link.get("requires_operation_id") or item["operation_id"])}
                            for item in supporting
                        )
                if home.ref not in found:
                    change = plan.change(emitter.page_id, emitter.slug, emitter.title)
                    if link not in change["removed_links"]:
                        change["removed_links"].append(link)


def _link_key(link: dict[str, Any]) -> tuple[str, str, str]:
    return (str(link.get("source") or ""), str(link.get("target") or ""), str(link.get("relation") or "related_to"))
