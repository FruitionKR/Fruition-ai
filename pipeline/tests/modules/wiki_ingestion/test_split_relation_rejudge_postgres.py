"""분리 때 부모의 개념 관계를 자식·부모 중 어느 쪽과 관련인지 다시 판정한다(ADR 0042 결정 6). 판정기는 가짜다."""
from __future__ import annotations

import pytest

from app.modules.wiki_ingestion.infrastructure import wiki_maintenance
from tests.modules.wiki_ingestion.test_concept_entry_lint_postgres import (  # noqa: F401
    pytestmark,
    World,
    _add,
    world,
)

ROLLOUT = [_add("e-rollout", "Rollout", refs=["R1"]),
           *(_add(f"e-r{index}", f"Step {index}", "e-rollout", [f"R{index + 1}"]) for index in range(5))]
RELATED = "concept_related_to"


class FakeJudge:
    """상대 개념 slug → 관련인 후보 slug 집합. 없으면(None) Jev를 쓸 수 없는 경우다."""

    def __init__(self, answers: dict[str, set[str] | None]) -> None:
        self.answers = answers
        self.asked: list[tuple[str, list[str]]] = []

    def judge_related(self, concept: dict, candidates: list[dict]) -> set[str] | None:
        self.asked.append((concept["slug"], sorted(item["slug"] for item in candidates)))
        return self.answers.get(concept["slug"])


@pytest.fixture
def judged(world: World, monkeypatch):
    def use(answers: dict[str, set[str] | None]) -> FakeJudge:
        judge = FakeJudge(answers)
        monkeypatch.setattr(wiki_maintenance, "jev_concept_judge", lambda: judge)
        return judge
    return use


def _relations(world: World) -> set[tuple[str, str]]:
    return {(source, target) for source, target, kind in world.state()["links"] if kind == RELATED}


def _setup(world: World) -> None:
    # doc-x가 Deployment와 Rollout 갈래를 쓰고, doc-c는 Canary → Deployment, doc-s는 Deployment → Service 관계를 낸다.
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "entry_changes": [dict(entry) for entry in ROLLOUT]}})
    world.ingest("op-s", "doc-s", {
        "deployment": {"title": "Deployment", "evidence": ["S1"],
                       "links": [{"source": "concept:deployment", "target": "concept:service", "relation": RELATED}]},
        "service": {"title": "Service", "evidence": ["S2"]},
    })
    world.ingest("op-c", "doc-c", {"canary": {"title": "Canary", "evidence": ["C1"],
                                             "links": [{"source": "concept:canary", "target": "concept:deployment",
                                                        "relation": RELATED}]}})
    assert _relations(world) == {("deployment", "service"), ("canary", "deployment")}


def test_relation_only_about_the_split_child_moves_to_it_and_split_restore_moves_it_back(world: World, judged) -> None:
    _setup(world)
    judge = judged({"canary": {"rollout"}, "service": {"rollout"}})

    world.lint("lint-split")

    assert sorted(judge.asked) == [("canary", ["deployment", "rollout"]), ("service", ["deployment", "rollout"])]
    assert _relations(world) == {("rollout", "service"), ("canary", "rollout")}
    for slug in ("canary", "deployment", "rollout"):
        assert world.state()["pages"][slug][2] == world.ledger_rebuild(slug).markdown

    world.restore_lint("lint-split")

    assert _relations(world) == {("deployment", "service"), ("canary", "deployment")}


def test_relation_about_both_keeps_the_parent_and_adds_the_child(world: World, judged) -> None:
    _setup(world)
    judged({"canary": {"deployment", "rollout"}, "service": {"deployment", "rollout"}})

    world.lint("lint-split")

    assert _relations(world) == {("deployment", "service"), ("rollout", "service"),
                                 ("canary", "deployment"), ("canary", "rollout")}


@pytest.mark.parametrize("answer", [{"deployment"}, None, set()], ids=["parent_only", "jev_unavailable", "neither"])
def test_relation_stays_when_it_is_about_the_parent_or_cannot_be_judged(world: World, judged, answer) -> None:
    _setup(world)
    judged({"canary": answer, "service": answer})

    world.lint("lint-split")

    assert _relations(world) == {("deployment", "service"), ("canary", "deployment")}


def test_moved_relation_disappears_when_the_ingest_that_made_it_is_rolled_back(world: World, judged) -> None:
    _setup(world)
    # Service·Canary는 다른 문서로도 받쳐져 관계를 낸 편입을 되돌려도 남는다.
    world.ingest("op-v", "doc-v", {"service": {"title": "Service", "evidence": ["V1"]},
                                   "canary": {"title": "Canary", "evidence": ["V2"]}})
    judged({"canary": {"rollout"}, "service": {"rollout"}})
    world.lint("lint-split")

    # 옮긴 링크는 원래 링크를 낸 편입에 종속된다.
    world.restore_ingest("op-c")
    assert "canary" in world.state()["pages"]
    assert _relations(world) == {("rollout", "service")}
    world.restore_ingest("op-s")
    assert "service" in world.state()["pages"]
    assert _relations(world) == set()
