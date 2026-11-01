"""lint 하위 항목 재검사(ADR 0042 3단계).

바뀐 페이지의 하위 항목으로 분리·여러 부모 승격·다시 합치기를 정하고, 그대로 재생할 기여 기록을 만든다.
- 분리·승격: 집 페이지에는 옮긴 항목마다 `remove`와 새 페이지를 가리키는 `entry_reference`를 남긴다. 새 페이지에는
  개념과 `split_from`을 남기고, 옮긴 항목의 원래 기록(편입, 클러스터 승격·병합 lint)을 원래 작업 id 그대로 잇는다(`linked`).
- 다시 합치기: 부모에 자식 항목마다 `revive`를 남겨 분리 때 지운 항목을 되살리고, 분리 뒤 자식에만 쌓인 편입 기록을
  부모에 잇는다. 자식은 지우고 자식 → 부모 방향 전환(`redirects`)을 남긴다.
- 다른 페이지의 참조는 옮긴 항목을 따라 대상을 바꾸고, 가리키던 항목이 없어진 참조는 지운다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.modules.wiki_ingestion.domain.concept_entries import (
    ENTRY_EVIDENCE_FIELDS,
    ENTRY_REFERENCE,
    _ancestors,
)
from app.modules.wiki_ingestion.domain.concept_split_rules import (
    EntryNode,
    decide_entry_changes,
)

# 편입이 아닌 기록. 수동 기록은 옮길 수 없고, lint 기록은 클러스터 승격·병합의 `add`만 옮길 근거를 가진다.
_LINT = "lint"
_MANUAL = "manual"


@dataclass(frozen=True)
class EntryPage:
    page_id: str
    slug: str
    title: str
    # 적용 순서의 원본 기여 JSON(전역화 전). 원래 작업 id로 다른 페이지에 이을 때 그대로 복사한다.
    contributions: tuple[dict[str, Any], ...]
    entries: tuple[dict[str, Any], ...]
    links: tuple[dict[str, Any], ...]
    restored_recently: bool = False

    @property
    def ref(self) -> str:
        return f"concept:{self.slug}"

    @property
    def split_record(self) -> dict[str, Any] | None:
        """이 페이지를 만든 분리 기록. 분리로 만든 자식 페이지만 가진다."""
        return next(
            (item for item in reversed(self.contributions) if isinstance(item.get("split_from"), dict)),
            None,
        )


@dataclass
class EntryLintPlan:
    decisions: list[dict[str, Any]] = field(default_factory=list)
    # page_id → 이번 lint 기록에 넣을 필드(slug·title·content_action·entry_changes·added_links·removed_links 등)
    page_changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 원래 작업 id 그대로 다른 페이지에 잇는 기여 JSON
    linked: list[dict[str, Any]] = field(default_factory=list)
    # 지운 자식 page_id → 부모 page_id
    redirects: dict[str, str] = field(default_factory=dict)
    # 롤백 직후라 이번에 합치지 않은 자식 페이지. 다음 lint가 다시 본다.
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
) -> EntryLintPlan:
    """pages는 바뀐 페이지와 참조로 이어진 이웃 페이지다. 판정은 바뀐 페이지만 한다.

    new_page(집 페이지, 항목) → (page_id, slug). 쓸 수 있는 slug가 없으면 None이다.
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
                    restored_recently=page.restored_recently,
                )
            )
            targets[node_id] = (page, None)
            if page.restored_recently:
                plan.deferred_page_ids.append(page.page_id)
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
        elif page.page_id in plan.redirects:
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
    _drop_dangling_references(plan, pages)
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
    if parent is None or parent.page_id in plan.redirects:
        return "parent_missing"
    # ponytail: 자식이 다른 페이지 항목을 참조하면(다시 나눈 손자 페이지 포함) 합치지 않는다.
    # 참조를 부모로 옮기고 손자의 부모를 바꾸는 처리는 필요해지면 더한다.
    if any(link.get("relation") == ENTRY_REFERENCE for link in child.links):
        return "has_references"
    linked = set(record.get("linked_operation_ids", []))
    parent_operations = {str(item.get("operation_id")) for item in parent.contributions}
    copies = []
    for contribution in child.contributions:
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
    change.setdefault("merged_pages", []).append({"page_id": child.page_id, "slug": child.slug})
    _retarget_references(
        plan,
        pages,
        child,
        {item["entry_id"] for item in child.entries},
        parent.ref,
        {parent.page_id, child.page_id},
    )
    # 자식에도 합친 기록을 남겨 lint 복원이 자식을 다시 만들 수 있게 한다. 자식이 내던 링크는 이 기록이 지운 것으로
    # 남겨 복원 때 되살린다. 다른 페이지에서 옮겨 간 링크(redirected_links)는 저장할 때 채운다.
    child_change = plan.change(child.page_id, child.slug, child.title)
    child_change["merged_into"] = {"page_id": parent.page_id, "slug": parent.slug}
    child_change["removed_links"].extend(link for link in child.links if link not in child_change["removed_links"])
    plan.redirects[child.page_id] = parent.page_id
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


def _drop_dangling_references(plan: EntryLintPlan, pages: dict[str, EntryPage]) -> None:
    """집 페이지는 남았는데 가리키던 항목이 없어진 참조를 지운다(1단계에서 남긴 공백)."""
    homes = {page.ref: page for page in pages.values()}
    for page in pages.values():
        for link in page.links:
            home = homes.get(str(link.get("target")))
            if link.get("relation") != ENTRY_REFERENCE or home is None:
                continue
            if link.get("entry_id") in {item["entry_id"] for item in home.entries}:
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
