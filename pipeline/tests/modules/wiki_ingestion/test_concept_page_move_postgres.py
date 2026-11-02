"""lint가 개념 페이지를 통째로 다른 페이지의 하위 항목으로 옮긴다(ADR 0042 4의 이동). 판정기·임베딩은 가짜다."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.modules.wiki_ingestion.domain.concept_entries import new_entry_id
from app.modules.wiki_ingestion.infrastructure import wiki_maintenance
from tests.modules.wiki_ingestion.test_concept_entry_lint_postgres import (  # noqa: F401
    pytestmark,
    World,
    _add,
    _reference,
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


class FakeModel:
    """모든 텍스트를 같은 벡터로 둔다. 프로세스 임베딩 캐시가 테스트끼리 섞이지 않게 모델 이름을 매번 다르게 한다."""

    def __init__(self, fail: bool = False) -> None:
        self.model_name = f"fake-{uuid4().hex}"
        self.fail = fail
        self.embedded: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("임베딩 서버 없음")
        self.embedded.extend(texts)
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def judged(world: World, monkeypatch):
    def use(answers: dict[tuple[str, str], float] | None, model: FakeModel | None = None) -> FakeJudge:
        judge = FakeJudge(answers)
        judge.model = model or FakeModel()
        monkeypatch.setattr(wiki_maintenance, "jev_concept_judge", lambda: judge)
        monkeypatch.setattr(wiki_maintenance, "embedding_server_model", lambda: judge.model)
        return judge
    return use


def _store_page_vectors(world: World, model: FakeModel, vectors: dict[str, list[float]]) -> None:
    """임베딩 작업이 끝나 저장된 페이지 벡터. 바뀌지 않은 페이지는 이 벡터로만 후보가 된다."""
    with world.connect() as conn:
        for slug, vector in vectors.items():
            conn.execute(
                "INSERT INTO wiki_page_embeddings (page_id, embedding_model, representation_hash, embedding_vector, "
                "embedding_dimension) VALUES (%s, %s, %s, %s, %s)",
                (world.page_id(slug), model.model_name, slug, vector, len(vector)),
            )


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


def test_move_keeps_all_accepted_parents_and_restores_them(world: World, judged) -> None:
    for slug in ("a", "b", "c", "detail"):
        world.ingest(f"op-{slug}", f"doc-{slug}", {slug: {"evidence": ["B1"]}})
    judged({(slug, "detail"): score for slug, score in (("a", .99), ("b", .95), ("c", .9))})
    before = world.state()

    world.lint("lint-multi")

    root = new_entry_id("a", "detail")
    assert {(row[0], row[1], row[2]) for row in world.state()["references"]} == {
        ("b", root, "a"), ("c", root, "a"),
    }
    world.restore_lint("lint-multi")
    assert world.state() == before


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
        judge = judged(MOVE_ANSWERS, FakeModel(fail=True))
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
    _store_page_vectors(world, judge.model, {slug: [1.0, 0.0] for slug in MOVERS})
    before = world.state()
    judge.asked.clear()
    judge.model.embedded.clear()

    result = world.lint("lint-move")

    # 임베딩은 바뀐 파드 페이지와 그 항목만 한다. 라이프사이클 페이지는 저장된 벡터로 찾는다.
    assert sorted(judge.model.embedded) == ["파드 (Pod) > Container: 컨테이너 정의", "파드 (Pod): pod 정의"]
    # 바뀐 파드 페이지에서 찾은 가까운 페이지가 후보다. 하위 항목이 있는 바뀐 파드 페이지도 옮길 후보다.
    assert {detail for _, detail in judge.asked} == {*MOVERS, "pod"}
    assert sorted(result["deleted_pages"]) == sorted(world.page_id(slug) for slug in MOVERS)
    state = world.state()
    assert set(state["pages"]) == {"pod"}
    assert (new_entry_id("pod", "container-probe"), "pod", "컨테이너 프로브", "e-container") in state["entries"]
    assert state["pages"]["pod"][2] == world.ledger_rebuild("pod").markdown
    assert ("doc-l", "pod") in state["documents"]

    world.restore_lint("lint-move")

    assert world.state() == before


def _manual_edit(world: World, slug: str) -> None:
    """사람이 고친 기록을 원장에 더한다. 그 페이지는 홀로 선 페이지가 아니다."""
    page_id = world.page_id(slug)
    operation_id = f"op-manual-{slug}"
    world.write(f"wiki/ws-entry/pages/{page_id}/ops/{operation_id}.json", json.dumps(
        {"schema_version": 1, "artifact_type": "manual", "operation_id": operation_id, "page_id": page_id,
         "added_links": [], "removed_links": []}))
    world.write(f"wiki/ws-entry/pages/{page_id}/ops/{operation_id}.md", f"# {slug}\n\n사람이 고친 본문\n")
    world._record(page_id, operation_id, None)


def test_reverse_search_skips_ineligible_nearest_pages_and_judges_eligible_ones_further_down(
    world: World, judged
) -> None:
    edited = [f"edited-{index}" for index in range(5)]
    world.ingest("op-l", "doc-l", {
        "pod-termination": {"title": "파드 종료", "evidence": ["L1"]},
        **{slug: {"title": slug, "evidence": [f"E{index}"]} for index, slug in enumerate(edited)},
    })
    for slug in edited:
        _manual_edit(world, slug)
    judge = judged({("pod", "pod-termination"): 0.95})
    world.lint("lint-first")
    world.ingest("op-p", "doc-p", {"pod": {"title": "파드 (Pod)", "evidence": ["P1"]}})
    # 파드(임베딩 [1, 0])에는 사람이 고친 페이지 5개가 파드 종료보다 가깝다. 파드 종료에는 파드가 가장 가깝다.
    _store_page_vectors(world, judge.model, {**{slug: [0.8, -0.6] for slug in edited}, "pod-termination": [0.6, 0.8]})
    judge.asked.clear()

    result = world.lint("lint-move")

    assert "pod-termination" in {detail for _, detail in judge.asked}
    assert not {detail for _, detail in judge.asked} & set(edited)
    assert result["deleted_pages"] == [world.page_id("pod-termination")]


def test_move_of_a_page_deleted_and_ingested_again_carries_only_records_after_the_delete(
    world: World, judged
) -> None:
    world.ingest("op-l", "doc-l", {"pod-termination": {"title": "파드 종료", "evidence": ["L1"]}})
    world.ingest("op-p", "doc-p", {"pod": {"title": "파드 (Pod)", "evidence": ["P1"]}})
    judge = judged({("pod", "pod-termination"): 0.95})
    world.lint("lint-move")
    # 옮겨 지운 페이지가 같은 slug로 다시 편입되고, 이번에는 노드 아래로 간다.
    world.ingest("op-l2", "doc-l2", {"pod-termination": {"title": "파드 종료", "evidence": ["M1"]}})
    world.ingest("op-n", "doc-n", {"node": {"title": "노드", "evidence": ["N1"]}})
    judge.answers = {("node", "pod-termination"): 0.95}
    before = world.state()

    result = world.lint("lint-again")

    assert result["deleted_pages"] == [world.page_id("pod-termination")]
    assert world.contribution("node", "op-l2")["entry_changes"][0]["name"] == "파드 종료"
    state = world.state()
    node, pod = state["pages"]["node"][2], state["pages"]["pod"][2]
    assert "doc-l2:M1" in node and "doc-l:L1" not in node
    assert "doc-l:L1" in pod
    assert node == world.ledger_rebuild("node").markdown
    assert ("doc-l", "node") not in state["documents"]

    world.restore_lint("lint-again")

    assert world.state() == before


def _pod_with_entries_then_workloads(world: World) -> None:
    """파드 문서가 하위 항목(컨테이너 > 초기화 컨테이너)을 가진 파드 페이지를 만들고, 서비스가 컨테이너 항목을 참조한다.
    뒤에 워크로드 문서가 들어왔다."""
    world.ingest("op-p", "doc-p", {"pod": {"title": "파드 (Pod)", "evidence": ["P1"], "entry_changes": [
        {**_add("e-container", "Container", refs=["P2"]), "definition": "컨테이너 정의"},
        {**_add("e-init", "Init Container", "e-container", ["P3"]), "definition": "초기화 컨테이너 정의"},
    ]}})
    world.ingest("op-s", "doc-s", {"service": {"title": "Service", "evidence": ["S1"], "links": [
        _reference("service", "pod", "e-container", "Container")]}})
    world.ingest("op-w", "doc-w", {"workloads": {"title": "워크로드", "evidence": ["W1"]}})


def test_page_with_sub_entries_moves_as_a_whole_and_references_follow(world: World, judged) -> None:
    _pod_with_entries_then_workloads(world)
    before = world.state()
    judged({("workloads", "pod"): 0.95})

    result = world.lint("lint-move")

    assert result["deleted_pages"] == [world.page_id("pod")]
    root = new_entry_id("workloads", "pod")
    state = world.state()
    assert set(state["pages"]) == {"workloads", "service"}
    assert {entry for entry in state["entries"] if entry[1] == "workloads"} == {
        (root, "workloads", "파드 (Pod)", None),
        ("e-container", "workloads", "Container", root),
        ("e-init", "workloads", "Init Container", "e-container"),
    }
    # 서비스의 참조는 같은 항목을 그대로 가리키고, 집 페이지만 워크로드로 바뀐다.
    assert ("service", "workloads", "entry_reference") in state["links"]
    assert not [link for link in state["links"] if "pod" in link[:2]]
    linked = world.contribution("workloads", "op-p")
    assert [change["entry_id"] for change in linked["entry_changes"]] == [root, "e-container", "e-init"]
    body = state["pages"]["workloads"][2]
    assert "doc-p:P2" in body and body == world.ledger_rebuild("workloads").markdown
    assert ("doc-p", "workloads") in state["documents"]

    world.restore_lint("lint-move")

    assert world.state() == before


def test_ingest_restore_after_whole_page_move_removes_its_entries_from_the_target(world: World, judged) -> None:
    _pod_with_entries_then_workloads(world)
    judged({("workloads", "pod"): 0.95})
    world.lint("lint-move")

    world.restore_ingest("op-p")

    state = world.state()
    assert [entry for entry in state["entries"] if entry[1] == "workloads"] == []
    assert ("doc-p", "workloads") not in state["documents"]


def test_page_is_not_moved_under_its_own_sub_entry(world: World, judged) -> None:
    _pod_with_entries_then_workloads(world)
    judged({("e-container", "pod"): 0.99})

    result = world.lint("lint-move")

    assert not [item for item in result["entry_decisions"] if item["action"] == "move"]


def test_whole_page_move_preserves_internal_and_target_page_reference_positions(world: World, judged) -> None:
    world.ingest("op-detail", "doc-detail", {"detail": {"evidence": ["D0"], "entry_changes": [
        _add("child", "Child", refs=["D1"]), _add("other", "Other", refs=["D2"]),
    ], "links": [{**_reference("detail", "detail", "child", "Child"), "parent_entry_id": "other"}]}})
    world.ingest("op-root", "doc-root", {"root": {"evidence": ["R0"], "entry_changes": [
        _add("parent-a", "Parent A", refs=["R1"]), _add("parent-b", "Parent B", refs=["R2"]),
    ], "links": [{**_reference("root", "detail", "child", "Child"), "parent_entry_id": parent}
                 for parent in ("parent-a", "parent-b")]}})
    before = world.state()
    judged({("root", "detail"): .99})

    world.lint("lint-move")

    refs = world.state()["references"]
    assert {(row[0], row[2], row[-1]) for row in refs} == {
        ("root", "root", parent) for parent in ("other", "parent-a", "parent-b")
    }
    assert len(world.ledger_rebuild("root").supported_links) == 3
    world.restore_lint("lint-move")
    assert world.state() == before
