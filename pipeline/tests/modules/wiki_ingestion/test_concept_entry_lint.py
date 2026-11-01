"""lint 하위 항목 재검사(ADR 0042 3단계)의 재생 규칙과 판정 기록. 전체 흐름은 _postgres 테스트가 본다."""
from __future__ import annotations

from app.modules.wiki_ingestion.domain.concept_entries import replay_concept_entries
from app.modules.wiki_ingestion.domain.concept_entry_lint import EntryPage, plan_entry_lint


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


def _page(page_id: str, contributions: list[dict], entries: list[dict], restored: bool = False) -> EntryPage:
    return EntryPage(page_id, page_id, page_id.title(), tuple(contributions), tuple(entries), (), restored)


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
    assert plan.page_changes == {} and plan.redirects == {}
    # 롤백 직후 자식은 판정하지 않고 다음 lint로 미룬다.
    restored = _page("rollout", [split], [], restored=True)
    assert plan_entry_lint({"rollout": restored, "deploy": parent}, {"rollout"}, lambda *_args: None).deferred_page_ids == [
        "rollout"]
