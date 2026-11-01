"""lint가 홀로 선 개념 페이지를 다른 페이지의 하위 항목으로 옮긴다(ADR 0042 4의 이동). 판정기·임베딩은 가짜다."""
from __future__ import annotations

import json

import pytest

from app.core.jev_client import JevUnavailable
from app.modules.wiki_ingestion.domain.concept_entries import new_entry_id
from app.modules.wiki_ingestion.infrastructure import wiki_maintenance
from tests.modules.wiki_ingestion.test_concept_entry_lint_postgres import (  # noqa: F401
    pytestmark,
    World,
    _add,
    _thick,
    world,
)

MOVERS = ("pod-termination", "container-probe", "pod-lifecycle")


class FakeJudge:
    """(상위, 세부) → P(part_of). 0.9 이상만 part_of다. 없으면 판정 결과 없음(Jev를 쓸 수 없음)이다."""

    def __init__(self, answers: dict[tuple[str, str], float] | None) -> None:
        self.answers = answers
        self.asked: list[tuple[str, str]] = []

    def judge_part_of(self, pairs):
        self.asked.extend((head["slug"], detail["slug"]) for head, detail in pairs)
        if self.answers is None:
            return {}
        return {
            (head["slug"], detail["slug"]): {
                "part_of": self.answers.get((head["slug"], detail["slug"]), 0.1) >= 0.9,
                "probability": self.answers.get((head["slug"], detail["slug"]), 0.1),
            }
            for head, detail in pairs
        }

    def judge_related(self, _concept, _candidates):
        return None


def _similarity(_query: str, texts: list[str]) -> list[float]:
    return [1.0] * len(texts)


@pytest.fixture
def judged(world: World, monkeypatch):
    def use(answers: dict[tuple[str, str], float] | None, similarity=_similarity) -> FakeJudge:
        judge = FakeJudge(answers)
        monkeypatch.setattr(wiki_maintenance, "jev_concept_judge", lambda: judge)
        monkeypatch.setattr(wiki_maintenance, "embedding_server_model", lambda: object())
        monkeypatch.setattr(wiki_maintenance, "embedding_similarity", lambda _model: similarity)
        return judge
    return use


def _lifecycle_then_pod(world: World) -> None:
    """라이프사이클 문서가 먼저 들어와 세부 개념이 각자 페이지가 됐고, 뒤에 파드 문서가 들어왔다."""
    world.ingest("op-l", "doc-l", {
        "pod-termination": {"title": "파드 종료", "evidence": ["L1"]},
        "container-probe": {"title": "컨테이너 프로브", "evidence": ["L2"]},
        "pod-lifecycle": {"title": "파드 라이프사이클", "evidence": ["L3"]},
    })
    world.ingest("op-s", "doc-s", {"service": {"title": "Service", "evidence": ["S1"], "links": [
        {"source": "concept:service", "target": "concept:pod-termination", "relation": "concept_related_to"}]}})
    world.ingest("op-p", "doc-p", {"pod": {"title": "파드 (Pod)", "evidence": ["P1"], "entry_changes": [
        {**_add("e-container", "Container", refs=["P2"]), "definition": "컨테이너 정의"}]}})


MOVE_ANSWERS = {
    ("pod", "pod-termination"): 0.95,
    ("pod", "pod-lifecycle"): 0.96,
    ("pod", "container-probe"): 0.92,
    ("e-container", "container-probe"): 0.97,
}


def test_standalone_pages_ingested_first_move_under_the_later_head_and_lint_restore_brings_them_back(
    world: World, judged
) -> None:
    _lifecycle_then_pod(world)
    before = world.state()
    judged(MOVE_ANSWERS)

    result = world.lint("lint-move")

    pod = world.page_id("pod")
    moved = {world.page_id(slug): slug for slug in MOVERS}
    assert {(item["action"], item["page_id"], item["target_page_id"], item["parent_entry_id"])
            for item in result["entry_decisions"]} == {
        ("move", world.page_id("pod-termination"), pod, None),
        ("move", world.page_id("pod-lifecycle"), pod, None),
        ("move", world.page_id("container-probe"), pod, "e-container"),
    }
    assert sorted(result["deleted_pages"]) == sorted(moved)
    # 옮긴 페이지에는 재생 가능한 삭제 기록, 대상 페이지에는 원래 편입 기록을 같은 작업 id로 잇는다(한 작업은 한 기록).
    for slug in MOVERS:
        assert world.contribution(slug, "lint-move")["content_action"] == "delete"
    linked = world.contribution("pod", "op-l")
    assert "concept" not in linked
    assert {(change["entry_id"], change["name"], change["parent_entry_id"]) for change in linked["entry_changes"]} == {
        (new_entry_id("pod", "pod-termination"), "파드 종료", None),
        (new_entry_id("pod", "pod-lifecycle"), "파드 라이프사이클", None),
        (new_entry_id("pod", "container-probe"), "컨테이너 프로브", "e-container"),
    }
    rows = [(row["page_id"], row["operation_id"], row["document_id"], row["linked_by_operation_id"])
            for row in result["ledger_contributions"]]
    assert (pod, "op-l", "doc-l", "lint-move") in rows
    assert {row[0] for row in rows if row[1] == "lint-move"} == {pod, world.page_id("service"), *moved}
    assert {page["page_id"] for page in result["changed_pages"]} >= {pod, *moved}

    state = world.state()
    assert set(state["pages"]) == {"pod", "service"}
    assert ("e-container", "pod", "Container", None) in state["entries"]
    assert (new_entry_id("pod", "container-probe"), "pod", "컨테이너 프로브", "e-container") in state["entries"]
    body = state["pages"]["pod"][2]
    assert "doc-l:L1" in body and "doc-l:L2" in body
    assert body == world.ledger_rebuild("pod").markdown
    assert ("doc-l", "pod") in state["documents"]
    assert not [row for row in state["documents"] if row[1] in MOVERS]
    # 옮긴 페이지로 오던 일반 링크는 고아로 정리한다.
    assert not [link for link in state["links"] if link[1] in MOVERS or link[0] in MOVERS]

    world.restore_lint("lint-move")

    assert world.state() == before


def test_ingest_restore_after_move_removes_the_moved_document_from_the_target(world: World, judged) -> None:
    _lifecycle_then_pod(world)
    judged(MOVE_ANSWERS)
    world.lint("lint-move")

    world.restore_ingest("op-l")

    state = world.state()
    assert set(state["pages"]) == {"pod", "service"}
    assert [entry for entry in state["entries"] if entry[1] == "pod"] == [("e-container", "pod", "Container", None)]
    assert "doc-l" not in state["pages"]["pod"][2]
    assert ("doc-l", "pod") not in state["documents"]


@pytest.mark.parametrize("case", ["unsure", "judge_unavailable", "embedding_unavailable", "llm_unsure"])
def test_no_move_without_a_confident_judgment(world: World, judged, monkeypatch, case: str) -> None:
    _lifecycle_then_pod(world)
    before = world.state()
    if case == "unsure":
        judged({pair: 0.6 for pair in MOVE_ANSWERS})
    elif case == "judge_unavailable":
        judged(None)
    elif case == "embedding_unavailable":
        def failing(_query, _texts):
            raise JevUnavailable("임베딩 실패")
        judge = judged(MOVE_ANSWERS, failing)
    else:
        # Jev가 꺼져 있으면 이 lint의 LLM이 같은 part_of 프롬프트로 답한다.
        class Completion:
            def complete_json(self, system_prompt, user_prompt):
                assert "Stage=ConceptPartOfJudge" in system_prompt
                return {"judgments": [{"id": item["id"], "decision": "part_of", "confidence": 0.6}
                                      for item in json.loads(user_prompt)["questions"]]}

        judged(MOVE_ANSWERS)
        monkeypatch.setattr(wiki_maintenance, "jev_concept_judge", lambda: None)
        monkeypatch.setattr(wiki_maintenance, "_lint_api_client", lambda _command: None)
        monkeypatch.setattr(wiki_maintenance, "RetryOnceJsonCompletion", lambda _client: Completion())

    result = world.lint("lint-move")

    assert not [item for item in result["entry_decisions"] if item["action"] == "move"]
    assert world.state() == before
    if case == "embedding_unavailable":
        assert judge.asked == []


def test_llm_judge_moves_when_jev_is_off(world: World, judged, monkeypatch) -> None:
    _lifecycle_then_pod(world)

    class Completion:
        def complete_json(self, _system_prompt, user_prompt):
            return {"judgments": [
                {"id": item["id"], "decision": "part_of", "confidence": 0.95}
                for item in json.loads(user_prompt)["questions"]
                if (item["head"]["slug"], item["detail"]["slug"]) == ("pod", "pod-termination")
            ]}

    judged(None)
    monkeypatch.setattr(wiki_maintenance, "jev_concept_judge", lambda: None)
    monkeypatch.setattr(wiki_maintenance, "_lint_api_client", lambda _command: None)
    monkeypatch.setattr(wiki_maintenance, "RetryOnceJsonCompletion", lambda _client: Completion())

    result = world.lint("lint-move")

    assert result["deleted_pages"] == [world.page_id("pod-termination")]
    assert set(world.state()["pages"]) == {"pod", "service", "container-probe", "pod-lifecycle"}


CHAIN_ANSWERS = {("pod", "pod-lifecycle"): 0.97, ("pod-lifecycle", "pod-termination"): 0.95,
                 (new_entry_id("pod", "pod-lifecycle"), "pod-termination"): 0.95}


def _chained(world: World, termination_operation: str) -> None:
    world.ingest("op-l", "doc-l", {"pod-lifecycle": {"title": "파드 라이프사이클", "evidence": ["L1"]}})
    world.ingest(termination_operation, f"doc-{termination_operation}",
                 {"pod-termination": {"title": "파드 종료", "evidence": ["T1"]}})
    world.ingest("op-p", "doc-p", {"pod": {"title": "파드 (Pod)", "evidence": ["P1"]}})


def test_a_page_moved_in_this_lint_is_not_a_target_until_the_next_lint(world: World, judged) -> None:
    _chained(world, "op-t")
    judged(CHAIN_ANSWERS)

    first = world.lint("lint-move")

    termination = world.page_id("pod-termination")
    assert first["deleted_pages"] == [world.page_id("pod-lifecycle")]
    assert {"action": "move", "page_id": termination, "entry_id": None, "skipped_reason": "chained"} in first[
        "entry_decisions"]
    assert termination in first["deferred_entry_pages"]
    assert "pod-termination" in world.state()["pages"]

    # 다음 lint에는 옮겨 간 라이프사이클이 파드 페이지의 항목이라 그 아래로 간다.
    second = world.lint("lint-next")

    lifecycle_entry = new_entry_id("pod", "pod-lifecycle")
    assert second["deleted_pages"] == [termination]
    assert (new_entry_id("pod", "pod-termination"), "pod", "파드 종료", lifecycle_entry) in world.state()["entries"]
    assert world.state()["pages"]["pod"][2] == world.ledger_rebuild("pod").markdown


def test_deferred_page_from_an_operation_already_on_the_target_is_not_moved(world: World, judged) -> None:
    # 한 문서가 두 페이지를 만들었고 하나만 먼저 옮겼다. 기록은 페이지·작업마다 하나라 둘째는 같은 작업 기록을 잇지 못한다.
    _chained(world, "op-l")
    judged(CHAIN_ANSWERS)
    world.lint("lint-move")

    second = world.lint("lint-next")

    assert [item.get("skipped_reason") for item in second["entry_decisions"]] == ["operation_on_both_pages"]
    assert "pod-termination" in world.state()["pages"]


def test_move_is_deferred_when_the_target_is_split_in_the_same_lint(world: World, judged) -> None:
    world.ingest("op-l", "doc-l", {"pod-termination": {"title": "파드 종료", "evidence": ["L1"]}})
    world.ingest("op-p", "doc-p", {"pod": {"title": "파드 (Pod)", "evidence": ["P1"],
                                           "entry_changes": _thick("e-spec", "Spec", 1)}})
    judged({("pod", "pod-termination"): 0.95})

    result = world.lint("lint-split")

    termination = world.page_id("pod-termination")
    assert ("split", "e-spec") in {(item["action"], item["entry_id"]) for item in result["entry_decisions"]}
    assert {"action": "move", "page_id": termination, "entry_id": None, "skipped_reason": "page_busy"} in result[
        "entry_decisions"]
    assert termination in result["deferred_entry_pages"]
    assert "pod-termination" in world.state()["pages"]

    result = world.lint("lint-next")

    assert result["deleted_pages"] == [termination]
    assert world.state()["pages"]["pod"][2] == world.ledger_rebuild("pod").markdown


def test_pages_linted_before_the_head_arrived_move_when_the_head_page_changes(world: World, judged) -> None:
    # 라이프사이클 문서 → lint → 파드 문서 순서. 라이프사이클 페이지는 둘째 lint에서 바뀐 페이지가 아니다.
    world.ingest("op-l", "doc-l", {
        "pod-termination": {"title": "파드 종료", "evidence": ["L1"]},
        "container-probe": {"title": "컨테이너 프로브", "evidence": ["L2"]},
        "pod-lifecycle": {"title": "파드 라이프사이클", "evidence": ["L3"]},
    })
    judge = judged(MOVE_ANSWERS)
    assert world.lint("lint-first")["deleted_pages"] == []
    world.ingest("op-p", "doc-p", {"pod": {"title": "파드 (Pod)", "evidence": ["P1"], "entry_changes": [
        {**_add("e-container", "Container", refs=["P2"]), "definition": "컨테이너 정의"}]}})
    before = world.state()
    judge.asked.clear()

    result = world.lint("lint-move")

    # 바뀐 파드 페이지에서 찾은 가까운 홀로 선 페이지가 후보다.
    assert {detail for _, detail in judge.asked} == set(MOVERS)
    assert sorted(result["deleted_pages"]) == sorted(world.page_id(slug) for slug in MOVERS)
    state = world.state()
    assert set(state["pages"]) == {"pod"}
    assert (new_entry_id("pod", "container-probe"), "pod", "컨테이너 프로브", "e-container") in state["entries"]
    assert state["pages"]["pod"][2] == world.ledger_rebuild("pod").markdown
    assert ("doc-l", "pod") in state["documents"]

    world.restore_lint("lint-move")

    assert world.state() == before
