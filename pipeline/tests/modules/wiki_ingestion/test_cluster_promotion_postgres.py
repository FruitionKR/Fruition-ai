"""클러스터 승격(ADR 0042 4단계)을 실제 PostgreSQL과 기록 재생으로 확인한다.

세부 항목이 P개 이상인 클러스터만 승격하고, 세부 항목은 승격 페이지(또는 병합 대상 개념)의 하위 항목이 된다.
document-svc 원장은 가짜이며 lint 결과의 `ledger_contributions`만 원장 행으로 더하고, 그 페이지가 `changed_pages`에 없으면 거절한다.
"""
from __future__ import annotations

from psycopg.types.json import Json

from app.modules.wiki_generation.infrastructure.jev_concept_resolver import JevConceptResolver
from app.modules.wiki_ingestion.application.models import WikiMaintenanceCommand
from app.modules.wiki_ingestion.domain.concept_entries import new_entry_id
from app.modules.wiki_ingestion.domain.concept_split_rules import MIN_CLUSTER_SUBCATEGORIES
from app.modules.wiki_ingestion.infrastructure import wiki_maintenance
from app.modules.wiki_ingestion.interfaces.http.schemas import WikiLintOut
from tests.modules.wiki_ingestion.test_concept_page_move_postgres import FakeModel, _store_page_vectors
from tests.modules.wiki_generation.test_jev_concept_resolver import AngleEmbedder, PairJev
from tests.modules.wiki_ingestion.test_concept_entry_lint_postgres import (  # noqa: F401
    WS,
    World,
    _add,
    pytestmark,
    world,
)

TERMS = ["Rollout History", "Rollout Undo", "Rollout Pause", "Rollout Resume", "Rollout Status"]


def _cluster(cluster_id: str, terms: list[str], target: str | None = None, start: int = 1) -> str:
    claims = "\n".join(
        f"- claim_{index:03d}: {term} - {term} 근거 문장이다. [doc-c:B{index:04d}]"
        for index, term in enumerate(terms, start=start)
    )
    return (
        f"## cluster: {cluster_id}\n\n### Evidence Claims\n{claims}\n\n"
        f"### Promotion\nstatus: candidate\nsource_refs: [doc-c]\n" + (f"target: concept:{target}\n" if target else "")
    )


def _thicken_history(world: World) -> None:
    """승격 페이지의 Rollout History 아래에 문서 doc-h가 세부 항목 5개를 더한다(분리 기준 C=5)."""
    history = next(entry["entry_id"] for entry in world.ledger_rebuild("rollout").entries if entry["name"] == "Rollout History")
    world.ingest("op-h", "doc-h", {"rollout": {"title": "Rollout", "entry_changes": [
        _add(f"e-h{index}", f"History {index}", history, [f"H{index}"]) for index in range(5)]}})


class PlacementJudge:
    """클러스터 승격 판정의 가짜 Jev. 같은 개념 판정은 없고, part_of는 (상위, 세부) → P(part_of)다."""

    def __init__(self, answers: dict[tuple[str, str], float]) -> None:
        self.answers = answers
        self.model = FakeModel()

    def judge_concepts(self, _concepts, _pool):
        return {}

    def judge_related(self, _concept, _candidates):
        return None

    def judge_part_of(self, pairs):
        return {
            (head["slug"], detail["slug"]): {
                "part_of": self.answers.get((head["slug"], detail["slug"]), 0.1) >= 0.9,
                "probability": self.answers.get((head["slug"], detail["slug"]), 0.1),
            }
            for head, detail in pairs
        }


class AmbiguousPromotionJudge:
    def __init__(self) -> None:
        self.client = PairJev()
        self.resolver = JevConceptResolver(self.client, None, AngleEmbedder())
        self.model = FakeModel()

    def judge_concepts(self, concepts, pool):
        return self.resolver.judge_concepts(concepts, pool)

    def judge_related(self, _concept, _candidates):
        return None

    def judge_part_of(self, pairs):
        return {
            (head["slug"], detail["slug"]): {"part_of": False, "probability": 0.0}
            for head, detail in pairs
        }


def _lint(
    world: World, monkeypatch, operation_id: str, active: str, judge: PlacementJudge | None = None, *, live_source: bool = True,
) -> dict:
    if live_source:
        world.add_source("doc-c")
    world.write(f"wiki/{WS}/clusters/active.md", "# Active Meaning Clusters\n\n" + active)
    monkeypatch.setattr(wiki_maintenance, "jev_concept_judge", lambda: judge)
    monkeypatch.setattr(wiki_maintenance, "embedding_server_model", lambda: judge.model if judge else None)
    monkeypatch.setattr(
        wiki_maintenance.PostgresWikiMaintenance,
        "_build_promotion_page_generator",
        lambda _self, _command: lambda cluster: {
            "slug": cluster["id"],
            "title": cluster["id"].title(),
            "markdown": f"# {cluster['id'].title()}\n\n## Definition\n{cluster['id']} 정의\n\n## Aliases\n-\n",
        },
    )
    run_id = world._run("lint", None)
    result = wiki_maintenance.PostgresWikiMaintenance().lint(
        WikiMaintenanceCommand(user_id="user", workspace_id=WS, provider="openai", model="gpt-5-nano",
                               operation_id=operation_id, materialize_promotions=True, dry_run=False)
    )
    result = WikiLintOut.model_validate(result).model_dump(mode="json")
    with world.connect() as conn:
        conn.execute("UPDATE pipeline_runs SET status = 'succeeded', manifest = %s WHERE id = %s",
                     (Json({"task_result": result}), run_id))
    changed = {page["page_id"] for page in result["changed_pages"]}
    for row in result["ledger_contributions"]:
        assert row["page_id"] in changed
        assert (row["operation_id"], row["document_id"], row["linked_by_operation_id"]) == (operation_id, None, None)
        world._record(row["page_id"], row["operation_id"], row["document_id"], row["linked_by_operation_id"])
    return result


def _sub_entry_units(world: World, slug: str) -> list[str]:
    with world.connect() as conn:
        rows = conn.execute(
            "SELECT unit.text FROM wiki_embedding_units unit JOIN wiki_pages page ON page.id = unit.page_id "
            "WHERE page.slug = %s AND unit.unit_type = 'sub_entry' ORDER BY unit.text",
            (slug,),
        ).fetchall()
    return [row["text"] for row in rows]


def test_cluster_below_p_detail_items_is_not_promoted(world: World, monkeypatch) -> None:
    result = _lint(world, monkeypatch, "lint-few", _cluster("rollout", TERMS[: MIN_CLUSTER_SUBCATEGORIES - 1]))

    assert result["materialized_promotions"] == []
    assert {"cluster_id": "rollout", "reason": "promotion candidate has too few detail items"} in result["invalid_promotions"]
    assert "rollout" not in world.state()["pages"]


def test_promoted_page_gets_detail_items_as_sub_entries_and_lint_restore_removes_it(world: World, monkeypatch) -> None:
    before = world.state()

    result = _lint(world, monkeypatch, "lint-promote", _cluster("rollout", TERMS))

    assert [item["concept_slug"] for item in result["materialized_promotions"]] == ["rollout"]
    contribution = world.contribution("rollout", "lint-promote")
    assert [(change["op"], change["name"]) for change in contribution["entry_changes"]] == [("add", term) for term in TERMS]
    assert contribution["entry_changes"][0]["evidence_claim_ids"] == ["claim_001"]
    assert contribution["entry_changes"][0]["anchor_reference_ids"] == ["doc-c:B0001"]
    assert contribution["entry_changes"][0]["definition"] == "Rollout History 근거 문장이다."
    after = world.state()
    assert sorted(name for _id, slug, name, _parent in after["entries"] if slug == "rollout") == sorted(TERMS)
    assert "## Sub-entries\n" + "".join(f"- {term}\n" for term in TERMS) in after["pages"]["rollout"][2]
    # 하위 항목마다 검색 단위가 생긴다. 기록 재생으로 다시 만들어도 같은 항목이 남는다.
    assert _sub_entry_units(world, "rollout") == sorted(f"Rollout > {term}: {term} 근거 문장이다." for term in TERMS)
    assert [entry["name"] for entry in world.ledger_rebuild("rollout").entries] == TERMS
    # 관련 개념 링크는 만들지 않는다.
    assert not [link for link in after["links"] if link[2] == "concept_related_to"]
    # 항목 근거의 문서가 승격 페이지의 문서 연결이 된다.
    assert ("doc-c", "rollout") in after["documents"]

    world.restore_lint("lint-promote")

    assert world.state() == before


def test_cluster_merged_into_core_concept_adds_distinct_detail_items_as_sub_entries(world: World, monkeypatch) -> None:
    world.ingest("op-x", "doc-x", {
        "deployment": {"title": "Deployment", "entry_changes": [_add("e-history", "Rollout History", refs=["B1"])]},
    })
    before = world.state()

    # 핵심 개념과 맞춘 클러스터는 개수와 무관하게 병합한다. 개념 자신의 이름은 항목이 아니고, 같은 이름의 항목에는 근거를 더한다.
    result = _lint(world, monkeypatch, "lint-merge-cluster",
                   _cluster("deploy-terms", ["Deployment", "rollout history", "Scale"], target="deployment"))

    assert [item["concept_slug"] for item in result["merged_promotions"]] == ["deployment"]
    contribution = world.contribution("deployment", "lint-merge-cluster")
    assert [(change["entry_id"], change["name"]) for change in contribution["entry_changes"]][0] == ("e-history", "rollout history")
    assert [change["name"] for change in contribution["entry_changes"]] == ["rollout history", "Scale"]
    entries = sorted(name for _id, slug, name, _parent in world.state()["entries"] if slug == "deployment")
    assert entries == ["Rollout History", "Scale"]
    rebuilt = {entry["name"]: entry for entry in world.ledger_rebuild("deployment").entries}
    assert rebuilt["Rollout History"]["evidence_claim_ids"] == ["op-x:ev-e-history", "lint-merge-cluster:claim_002"]

    world.restore_lint("lint-merge-cluster")

    assert world.state() == before


def test_lint_sub_entries_on_merged_concept_survive_restore_of_an_unrelated_ingest(world: World, monkeypatch) -> None:
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "entry_changes": [_add("e-scale", "Scale", refs=["B1"])]}})
    _lint(world, monkeypatch, "lint-merge-cluster", _cluster("deploy-terms", ["Rollout History", "Rollout Undo"], target="deployment"))
    world.ingest("op-y", "doc-y", {"deployment": {"title": "Deployment", "entry_changes": [_add("e-pause", "Pause", refs=["B2"])]}})

    world.restore_ingest("op-y")

    state = world.state()
    entries = sorted(name for _id, slug, name, _parent in state["entries"] if slug == "deployment")
    assert entries == ["Rollout History", "Rollout Undo", "Scale"]
    assert ("doc-c", "deployment") in state["documents"]
    assert ("doc-y", "deployment") not in state["documents"]


def test_split_of_promoted_entry_links_the_promotion_record_and_restores_with_it(world: World, monkeypatch) -> None:
    _lint(world, monkeypatch, "lint-promote", _cluster("rollout", [*TERMS, *["Rollout History"] * 4]))
    _thicken_history(world)

    result = world.lint("lint-split")

    assert [(item["action"], item["skipped_reason"] if "skipped_reason" in item else None)
            for item in result["entry_decisions"]] == [("split", None)]
    # 옮긴 항목을 만든 승격 기록을 원래 작업 id 그대로 자식에 잇는다.
    linked = world.contribution("rollout-history", "lint-promote")
    assert linked["linked_by_operation_id"] == "lint-split"
    assert [change["name"] for change in linked["entry_changes"]] == ["Rollout History"]
    assert len(linked["evidence_units"]) == 5
    assert {"page_id": world.page_id("rollout-history"), "operation_id": "lint-promote", "document_id": None,
            "contribution_key": f"wiki/{WS}/pages/{world.page_id('rollout-history')}/ops/lint-promote.json",
            "linked_by_operation_id": "lint-split"} in result["ledger_contributions"]
    state = world.state()
    assert ("rollout-history" in state["pages"]) and state["pages"]["rollout-history"][2] == world.ledger_rebuild("rollout-history").markdown
    assert [entry["name"] for entry in world.ledger_rebuild("rollout-history").entries] == [
        "Rollout History", *(f"History {index}" for index in range(5))]
    assert ("doc-c", "rollout-history") in state["documents"]

    # 세부 항목 편입과 승격 lint를 되돌리면 부모의 항목과 자식에 이은 기록이 함께 빠진다.
    world.restore_ingest("op-h")
    world.restore_lint("lint-promote")

    state = world.state()
    assert state["entries"] == []
    assert "rollout" not in state["pages"]
    assert not [document for document in state["documents"] if document[1] == "rollout-history"]


def test_detail_items_that_differ_only_by_symbols_stay_separate_entries(world: World, monkeypatch) -> None:
    names = ["C", "C++", "C#", "Java", "Python"]

    _lint(world, monkeypatch, "lint-languages", _cluster("languages", names))

    entries = world.ledger_rebuild("languages").entries
    assert [entry["name"] for entry in entries] == names
    assert len({entry["entry_id"] for entry in entries}) == 5
    assert [entry["anchor_reference_ids"] for entry in entries] == [[f"doc-c:B{index:04d}"] for index in range(1, 6)]


def test_restoring_a_split_of_a_promoted_page_keeps_the_page_its_lint_record_supports(world: World, monkeypatch) -> None:
    # 승격 페이지는 lint 기록만으로 받쳐진다. 그 항목의 분리를 되돌려도 승격 근거가 남아 페이지는 남는다.
    _lint(world, monkeypatch, "lint-promote", _cluster("rollout", [*TERMS, *["Rollout History"] * 4]))
    _thicken_history(world)
    assert [item["action"] for item in world.lint("lint-split")["entry_decisions"]] == ["split"]

    result = world.restore_lint("lint-split")

    state = world.state()
    assert world.page_id("rollout") not in result["deleted_pages"]
    assert "rollout" in state["pages"] and "rollout-history" not in state["pages"]
    assert "Rollout History" in [entry[2] for entry in state["entries"] if entry[1] == "rollout"]


def test_promoted_concept_judged_part_of_an_existing_page_becomes_its_sub_entry(world: World, monkeypatch) -> None:
    world.ingest("op-d", "doc-d", {"deployment": {"title": "Deployment", "evidence": ["D1"]}})
    judge = PlacementJudge({("deployment", "rollout"): 0.95})
    _store_page_vectors(world, judge.model, {"deployment": [1.0, 0.0]})
    before = world.state()

    result = _lint(world, monkeypatch, "lint-place", _cluster("rollout", TERMS), judge)

    assert result["materialized_promotions"] == []
    root = new_entry_id("deployment", "rollout")
    assert result["merged_promotions"] == [
        {"cluster_id": "rollout", "concept_slug": "deployment", "page_id": world.page_id("deployment"), "entry_id": root}]
    contribution = world.contribution("deployment", "lint-place")
    # 승격 개념은 항목에만 있다. 상위 페이지의 이름·정의는 그대로다.
    assert contribution["concept"]["title"] == "Deployment"
    assert contribution["concept"]["definition"] == "deployment 정의"
    assert contribution["entry_changes"][0]["entry_id"] == root
    assert contribution["entry_changes"][0]["name"] == "Rollout"
    assert contribution["entry_changes"][0]["parent_entry_id"] is None
    assert len(contribution["entry_changes"][0]["evidence_claim_ids"]) == len(TERMS)
    assert [(change["name"], change["parent_entry_id"]) for change in contribution["entry_changes"][1:]] == [
        (term, root) for term in TERMS]
    state = world.state()
    assert "rollout" not in state["pages"]
    assert state["pages"]["deployment"][0] == "Deployment"
    assert sorted(name for _id, slug, name, parent in state["entries"] if slug == "deployment" and parent == root) == sorted(TERMS)
    assert [(entry["name"], entry["parent_entry_id"]) for entry in world.ledger_rebuild("deployment").entries] == [
        ("Rollout", None), *((term, root) for term in TERMS)]
    assert ("doc-c", "deployment") in state["documents"]

    world.restore_lint("lint-place")

    assert world.state() == before


def test_promotion_without_a_confident_parent_stays_a_new_page(world: World, monkeypatch) -> None:
    world.ingest("op-d", "doc-d", {"deployment": {"title": "Deployment", "evidence": ["D1"]}})
    judge = PlacementJudge({("deployment", "rollout"): 0.6})
    _store_page_vectors(world, judge.model, {"deployment": [1.0, 0.0]})

    result = _lint(world, monkeypatch, "lint-new", _cluster("rollout", TERMS), judge)

    assert [item["concept_slug"] for item in result["materialized_promotions"]] == ["rollout"]
    assert result["merged_promotions"] == []


def test_ambiguous_promotion_aliases_skip_jev_and_allocate_a_new_page(world: World, monkeypatch) -> None:
    world.ingest("op-one", "doc-one", {
        "one": {"title": "One", "identity_aliases": ["Shared"], "evidence": ["B1"]},
    })
    world.ingest("op-two", "doc-two", {
        "two": {"title": "Two", "identity_aliases": ["Shared"], "evidence": ["B1"]},
    })
    with world.connect() as conn:
        row = conn.execute(
            "UPDATE wiki_pages SET summary = '' WHERE workspace_id = %s AND slug = 'two' RETURNING markdown_uri",
            (WS,),
        ).fetchone()
    world.write(row["markdown_uri"], "# Two\n\n## Definition\n\n")
    judge = AmbiguousPromotionJudge()

    result = _lint(world, monkeypatch, "lint-shared", _cluster("shared", TERMS), judge)

    assert judge.client.requests == []
    assert result["materialized_promotions"] == [{
        "cluster_id": "shared", "concept_slug": "shared", "page_id": world.page_id("shared"),
    }]
    assert result["merged_promotions"] == []
    assert set(world.state()["pages"]) >= {"one", "two", "shared"}


def test_promotion_keeps_all_accepted_parents_and_restore_removes_references(world: World, monkeypatch) -> None:
    for slug in ("a", "b", "c"):
        world.ingest(f"op-{slug}", f"doc-{slug}", {slug: {"evidence": ["B1"]}})
    judge = PlacementJudge({(slug, "rollout"): score for slug, score in (("a", .99), ("b", .95), ("c", .9))})
    _store_page_vectors(world, judge.model, {slug: [1., 0.] for slug in ("a", "b", "c")})
    before = world.state()

    _lint(world, monkeypatch, "lint-multi", _cluster("rollout", TERMS), judge)

    root = new_entry_id("a", "rollout")
    assert {(row[0], row[1], row[2]) for row in world.state()["references"]} == {
        ("b", root, "a"), ("c", root, "a"),
    }
    for slug in ("b", "c"):
        assert world.ledger_rebuild(slug).supported_links[0]["entry_id"] == root
    world.restore_lint("lint-multi")
    assert world.state() == before


def test_two_clusters_into_one_page_in_one_lint_keep_both_records(world: World, monkeypatch) -> None:
    world.ingest("op-d", "doc-d", {"deployment": {"title": "Deployment", "evidence": ["D1"]}})
    before = world.state()
    first = _cluster("deploy-a", ["Scale", "Pause"], target="deployment")
    second = _cluster("deploy-b", ["Resume", "Status"], target="deployment", start=3)

    result = _lint(world, monkeypatch, "lint-two", first + "\n\n" + second)

    assert [item["cluster_id"] for item in result["merged_promotions"]] == ["deploy-a", "deploy-b"]
    contribution = world.contribution("deployment", "lint-two")
    assert [change["name"] for change in contribution["entry_changes"]] == ["Scale", "Pause", "Resume", "Status"]
    assert [unit["evidence_id"] for unit in contribution["evidence_units"]] == [
        "claim_001", "claim_002", "claim_003", "claim_004"]
    assert [item["cluster_id"] for item in contribution["archived_clusters"]] == ["deploy-a", "deploy-b"]
    state = world.state()
    assert sorted(name for _id, slug, name, _parent in state["entries"] if slug == "deployment") == [
        "Pause", "Resume", "Scale", "Status"]
    assert [entry["name"] for entry in world.ledger_rebuild("deployment").entries] == ["Scale", "Pause", "Resume", "Status"]

    world.restore_lint("lint-two")

    assert world.state() == before


def test_promoted_parent_becomes_a_page_and_the_next_lint_moves_existing_pages_under_it(world: World, monkeypatch) -> None:
    # 기존 페이지(하위 항목 있음)의 상위 개념이 클러스터로 모여 승격된다. 승격은 새 페이지, 다음 lint가 기존 페이지를 옮긴다.
    world.ingest("op-s", "doc-s", {"strategy": {"title": "Strategy", "evidence": ["S1"], "entry_changes": [
        {**_add("e-blue", "Blue Green", refs=["S2"]), "definition": "블루 그린 정의"}]}})
    judge = PlacementJudge({("rollout", "strategy"): 0.95})

    class NearModel(FakeModel):
        """승격 개념(Rollout)과 Strategy만 가깝다. 승격 페이지의 세부 항목은 멀다."""

        def embed(self, texts):
            return [[1.0, 0.0] if text.startswith(("Rollout:", "Strategy")) else [0.0, 1.0] for text in texts]

    judge.model = NearModel()
    _store_page_vectors(world, judge.model, {"strategy": [1.0, 0.0]})

    promoted = _lint(world, monkeypatch, "lint-promote", _cluster("rollout", TERMS), judge)

    assert [item["concept_slug"] for item in promoted["materialized_promotions"]] == ["rollout"]
    assert "strategy" in world.state()["pages"]

    moved = world.lint("lint-move")

    assert moved["deleted_pages"] == [world.page_id("strategy")]
    root = new_entry_id("rollout", "strategy")
    entries = {entry for entry in world.state()["entries"] if entry[1] == "rollout"}
    assert (root, "rollout", "Strategy", None) in entries
    assert ("e-blue", "rollout", "Blue Green", root) in entries


def test_lint_with_promotion_still_deletes_concept_without_source(world: World, monkeypatch) -> None:
    world.ingest("op-a", "doc-a", {"alpha": {"evidence": ["A1"]}})
    world.mark_linted()
    world.trash("doc-a")
    alpha = world.page_id("alpha")

    result = _lint(world, monkeypatch, "lint-both", _cluster("rollout", TERMS))

    state = world.state()
    assert [item["concept_slug"] for item in result["materialized_promotions"]] == ["rollout"]
    assert "rollout" in state["pages"]
    assert "alpha" not in state["pages"]
    with world.connect() as conn:
        linked = conn.execute(
            "SELECT 1 FROM wiki_page_links WHERE to_page_id = %s AND link_type = 'source_mentions_concept'",
            (world.page_id("rollout"),),
        ).fetchone()
    assert linked is not None
    assert result["deleted_pages"] == [alpha]
    assert world.contribution("alpha", "lint-both")["content_action"] == "delete"
    assert world.contribution("rollout", "lint-both")["content_action"] == "create"


def test_promoted_page_without_live_source_is_deleted_in_the_same_lint(world: World, monkeypatch) -> None:
    # 근거 문서의 소스 페이지가 없으면 승격이 소스 → 개념 링크를 잇지 못한다.
    result = _lint(world, monkeypatch, "lint-promote", _cluster("rollout", TERMS), live_source=False)

    assert [item["concept_slug"] for item in result["materialized_promotions"]] == ["rollout"]
    assert "rollout" not in world.state()["pages"]
    assert result["deleted_pages"] == [world.page_id("rollout")]
    assert world.contribution("rollout", "lint-promote")["content_action"] == "delete"
    assert world.page_id("rollout") in [row["page_id"] for row in result["ledger_contributions"]]
