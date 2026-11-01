"""클러스터 승격(ADR 0042 4단계)을 실제 PostgreSQL과 기록 재생으로 확인한다.

세부 항목이 P개 이상인 클러스터만 승격하고, 세부 항목은 승격 페이지(또는 병합 대상 개념)의 하위 항목이 된다.
document-svc 원장은 가짜이며 lint 결과의 `ledger_contributions`만 원장 행으로 더하고, 그 페이지가 `changed_pages`에 없으면 거절한다.
"""
from __future__ import annotations

from psycopg.types.json import Json

from app.modules.wiki_ingestion.application.models import WikiMaintenanceCommand
from app.modules.wiki_ingestion.domain.concept_split_rules import MIN_CLUSTER_SUBCATEGORIES
from app.modules.wiki_ingestion.infrastructure import wiki_maintenance
from app.modules.wiki_ingestion.interfaces.http.schemas import WikiLintOut
from tests.modules.wiki_ingestion.test_concept_entry_lint_postgres import (  # noqa: F401
    WS,
    World,
    _add,
    pytestmark,
    world,
)

TERMS = ["Rollout History", "Rollout Undo", "Rollout Pause", "Rollout Resume", "Rollout Status"]


def _cluster(cluster_id: str, terms: list[str], target: str | None = None) -> str:
    claims = "\n".join(
        f"- claim_{index:03d}: {term} - {term} 근거 문장이다. [doc-c:B{index:04d}]"
        for index, term in enumerate(terms, start=1)
    )
    return (
        f"## cluster: {cluster_id}\n\n### Evidence Claims\n{claims}\n\n"
        f"### Promotion\nstatus: candidate\nsource_refs: [doc-c]\n" + (f"target: concept:{target}\n" if target else "")
    )


def _lint(world: World, monkeypatch, operation_id: str, active: str) -> dict:
    world.write(f"wiki/{WS}/clusters/active.md", "# Active Meaning Clusters\n\n" + active)
    monkeypatch.setattr(wiki_maintenance, "jev_concept_judge", lambda: None)
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
    known = {row["page_id"] for row in world.ledger}
    changed = {page["page_id"] for page in result["changed_pages"]}
    for row in result["ledger_contributions"]:
        assert row["page_id"] in changed
        assert (row["operation_id"], row["document_id"], row["linked_by_operation_id"]) == (operation_id, None, None)
        world._record(row["page_id"], row["operation_id"], row["document_id"], row["linked_by_operation_id"])
    world.created[operation_id] = sorted({row["page_id"] for row in result["ledger_contributions"]} - known)
    world.deactivated[operation_id] = []
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
