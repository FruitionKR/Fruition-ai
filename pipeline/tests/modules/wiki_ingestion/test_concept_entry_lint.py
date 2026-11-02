"""lint 하위 항목 재검사(ADR 0042 3단계)의 재생 규칙과 판정 기록. 전체 흐름은 _postgres 테스트가 본다."""
from __future__ import annotations

from app.modules.wiki_ingestion.domain.concept_entries import replay_concept_entries
from app.modules.wiki_ingestion.domain.concept_entry_lint import EntryLintPlan, EntryPage, move_page_under, plan_entry_lint
from app.modules.wiki_ingestion.domain.orphan_link_lint import replay_supported_links


def _add(entry_id: str, parent: str | None = None, refs: list[str] | None = None) -> dict:
    return {"op": "add", "entry_id": entry_id, "name": entry_id, "parent_entry_id": parent,
            "anchor_reference_ids": refs or [], "evidence_claim_ids": []}


def test_revive_restores_entries_removed_by_split_with_their_evidence() -> None:
    added = {"entry_changes": [_add("e-rollout", refs=["d:B1"]), _add("e-history", "e-rollout", ["d:B2"])]}
    split = {"entry_changes": [{"op": "remove", "entry_id": "e-rollout"}, {"op": "remove", "entry_id": "e-history"}]}
    merged = {"entry_changes": [{"op": "revive", "entry_id": "e-rollout"}, {"op": "revive", "entry_id": "e-history"}]}

    assert replay_concept_entries([added, split]) == []
    assert replay_concept_entries([added, split, merged]) == replay_concept_entries([added])
    # 항목을 만든 기여가 빠지면 되살릴 것도 없다.
    assert replay_concept_entries([split, merged]) == []


def _page(page_id: str, contributions: list[dict], entries: list[dict]) -> EntryPage:
    return EntryPage(page_id, page_id, page_id.title(), tuple(contributions), tuple(entries), ())


def test_merge_back_is_skipped_when_one_operation_contributed_to_parent_and_child() -> None:
    split = {"artifact_type": "lint", "operation_id": "lint-1", "linked_operation_ids": ["op-x"],
             "split_from": {"page_id": "deploy", "slug": "deploy", "entry_id": "e-rollout"}}
    after_split = {"operation_id": "op-y", "entry_changes": [_add("e-undo")]}
    child = _page("rollout", [split, after_split], [{"entry_id": "e-undo", "name": "Undo", "parent_entry_id": None,
                                                    "anchor_reference_ids": ["d:B1"], "evidence_claim_ids": []}])
    parent = _page("deploy", [{"operation_id": "op-x"}, {"operation_id": "op-y"}], [])

    plan = plan_entry_lint({"rollout": child, "deploy": parent}, {"rollout"}, lambda *_args: None)

    assert plan.decisions == [{"action": "merge_back", "page_id": "rollout", "entry_id": None,
                               "skipped_reason": "operation_on_both_pages"}]
    assert plan.page_changes == {} and plan.deleted_page_ids == []


def test_split_moves_references_placed_under_the_moved_subtree() -> None:
    reference = {"source": "concept:home", "target": "concept:other", "relation": "entry_reference",
                 "entry_id": "x-other", "name": "Other", "parent_entry_id": "e-child-0"}
    entries = [{"entry_id": "e-root", "name": "Root", "parent_entry_id": None,
                "anchor_reference_ids": [], "evidence_claim_ids": []}]
    entries += [{"entry_id": f"e-child-{index}", "name": f"Child {index}", "parent_entry_id": "e-root",
                 "anchor_reference_ids": [], "evidence_claim_ids": []} for index in range(5)]
    home = EntryPage("home", "home", "Home", ({"operation_id": "op-x"},), tuple(entries), (reference,))

    plan = plan_entry_lint({"home": home}, {"home"}, lambda *_args: ("root-page", "root"))

    assert reference in plan.page_changes["home"]["removed_links"]
    assert {**reference, "source": "concept:root"} in plan.page_changes["root-page"]["added_links"]


def test_moving_pages_keeps_reference_removals_when_shared_operations_are_combined() -> None:
    target = _page("target", [], [])
    plan = EntryLintPlan()
    for slug in ("a", "b"):
        reference = {"source": f"concept:{slug}", "target": f"concept:{slug}",
                     "relation": "entry_reference", "entry_id": f"child-{slug}", "parent_entry_id": None}
        source = _page(slug, [
            {"operation_id": "ingest", "entry_changes": [_add(f"child-{slug}")], "links": [reference]},
            {"operation_id": "remove", "artifact_type": "lint", "removed_links": [reference]},
        ], [])
        assert move_page_under(plan, {slug: source, "target": target}, source, slug, target, None) is None

    assert len(next(item for item in plan.linked if item["operation_id"] == "remove")["removed_links"]) == 2
    assert replay_supported_links(plan.linked) == []
