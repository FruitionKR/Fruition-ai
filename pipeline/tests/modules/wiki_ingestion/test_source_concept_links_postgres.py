"""소스 → 개념 자동 링크(`source_mentions_concept`)가 재생 상태에서 파생되는지 실제 PostgreSQL로 본다(ADR 0042 결정 5).

"문서 D의 소스 페이지 → 개념 P 링크가 있다 iff P의 재생 상태에 D의 유효한 직접 개념 기여 또는 살아 있는 항목·근거가 있다."
사람이 소스 본문에 쓴 `[[slug]]`는 자동 지원과 따로 남고, 수동 삭제가 자동보다 우선한다.
"""
from __future__ import annotations

import pytest

from app.modules.wiki_ingestion.application.models import SourceSnapshotRestoreCommand
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import sync_source_concept_links
from app.modules.wiki_ingestion.infrastructure.source_contribution_reconciliation import (
    apply_structural_reconciliation,
)
from tests.modules.wiki_ingestion.test_concept_entry_lint_postgres import (  # noqa: F401
    pytestmark,
    WS,
    World,
    _add,
    world,
)

ROLLOUT = [_add("e-rollout", "Rollout", refs=["R1"]),
           *(_add(f"e-r{index}", f"Step {index}", "e-rollout", [f"R{index + 1}"]) for index in range(5))]


@pytest.fixture(autouse=True)
def _explicit_sources(world: World) -> None:
    world.auto_source = False


def _source(world: World, document_id: str, markdown: str = "") -> str:
    """편입이 만드는 소스 페이지와 `source_of` 문서 연결."""
    with world.connect() as conn:
        page_id = database._resolve_or_create_wiki_page_id(conn, "user", WS, "source", document_id)
        key = f"wiki/{WS}/pages/{page_id}/ops/src-{document_id}.md"
        world.write(key, markdown or f"# {document_id}\n")
        database._upsert_wiki_page(conn, page_id, "source", document_id, document_id, "", key, "user", WS)
        database._upsert_document_wiki_link(conn, document_id, page_id, "source_of", 1.0, WS)
    world._record(page_id, f"src-{document_id}", document_id)
    return page_id


def _links(world: World) -> set[tuple[str, str]]:
    return {(source, target) for source, target, kind in world.state()["links"] if kind == "source_mentions_concept"}


def _refs(entries: list[dict]) -> list[str]:
    return [ref for entry in entries for ref in entry["anchor_reference_ids"]]


def test_split_moves_the_source_link_to_the_child_and_split_restore_moves_it_back(world: World) -> None:
    for document_id in ("doc-x", "doc-r"):
        _source(world, document_id)
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "concept_refs": ["B9"],
                                                  "entry_changes": [_add("e-scale", "Scale", refs=["B9"])]}})
    # doc-r는 Deployment의 하위 항목(Rollout 갈래)만 말한다.
    world.ingest("op-r", "doc-r", {"deployment": {"title": "Deployment", "concept_refs": _refs(ROLLOUT),
                                                  "entry_changes": [dict(entry) for entry in ROLLOUT]}})
    assert _links(world) == {("doc-x", "deployment"), ("doc-r", "deployment")}

    world.lint("lint-split")

    # 하위 개념만 그 문서와 관계있으면 부모 링크를 풀고 자식으로 잇는다.
    assert _links(world) == {("doc-x", "deployment"), ("doc-r", "rollout")}

    world.restore_lint("lint-split")

    assert _links(world) == {("doc-x", "deployment"), ("doc-r", "deployment")}


def test_merge_moves_the_source_link_to_the_parent_and_merge_restore_moves_it_back(world: World) -> None:
    for document_id in ("doc-x", "doc-d", "doc-y"):
        _source(world, document_id)
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "concept_refs": _refs(ROLLOUT),
                                                  "entry_changes": [dict(entry) for entry in ROLLOUT]}})
    world.ingest("op-d", "doc-d", {"deployment": {"title": "Deployment", "evidence": ["B99"]}})
    world.ingest("op-y", "doc-y", {"deployment": {"title": "Deployment", "concept_refs": ["B20"],
                                                  "entry_changes": [_add("e-history", "History", "e-rollout", ["B20"])]}})
    world.lint("lint-split")
    assert ("doc-y", "rollout") in _links(world) and ("doc-y", "deployment") not in _links(world)
    world.restore_ingest("op-x")
    assert not [link for link in _links(world) if link[0] == "doc-x"]

    world.lint("lint-merge")

    assert _links(world) == {("doc-d", "deployment"), ("doc-y", "deployment")}

    world.restore_lint("lint-merge")

    # 취소한 doc-x는 되살아나지 않는다.
    assert _links(world) == {("doc-d", "deployment"), ("doc-y", "rollout")}


def test_tombstoned_document_is_not_linked_until_it_is_ingested_again(world: World) -> None:
    source_id = _source(world, "doc-x")
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "evidence": ["B1"]}})
    world.ingest("op-w", "doc-w", {"service": {"title": "Service", "evidence": ["B2"]}})
    with world.connect() as conn:
        conn.execute("INSERT INTO wiki_source_tombstones (workspace_id, document_id) VALUES (%s, 'doc-x')", (WS,))

    # 휴지통 문서는 다시 재생해도 잇지 않는다.
    page = world._rebuild("resync", world.page_id("deployment"))
    database.apply_restored_wiki_state(WS, [page], {"removed_links": [], "restored_links": page["supported_links"]}, True)
    assert _links(world) == set()

    # 다시 편입하면(재활성화) 이번에 바뀌지 않은 개념과의 연결도 소스 페이지 범위로 맞춘다.
    with world.connect() as conn:
        conn.execute("DELETE FROM wiki_source_tombstones WHERE document_id = 'doc-x'")
        conn.execute(
            "INSERT INTO document_wiki_links (document_id, wiki_page_id, relation_type, workspace_id, created_at) "
            "VALUES ('doc-x', %s, 'extracted_concept', %s, now()) ON CONFLICT DO NOTHING",
            (world.page_id("deployment"), WS),
        )
        sync_source_concept_links(conn, WS, source_page_ids=[source_id])
        sync_source_concept_links(conn, WS, source_page_ids=[source_id])
    assert _links(world) == {("doc-x", "deployment")}


def _manual_edit(world: World, source_id: str, operation_id: str, markdown: str) -> None:
    assert database.save_manual_wiki_edit(source_id, "user", WS, operation_id, markdown) is not None
    world._record(source_id, operation_id, None)


def test_manual_source_links_are_kept_apart_from_derived_ones(world: World) -> None:
    source_id = _source(world, "doc-x", "# doc-x\n")
    _source(world, "doc-m", "# doc-m\n")
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "evidence": ["B1"]}})
    world.ingest("op-d", "doc-d", {"deployment": {"title": "Deployment", "evidence": ["B2"]},
                                   "service": {"title": "Service", "evidence": ["B3"]}})
    # 자동 링크와 겹치는 수동 링크, 그리고 근거 없는 수동 전용 링크.
    _manual_edit(world, source_id, "op-m1", "# doc-x\n[[deployment]] [[service]]\n")
    assert _links(world) == {("doc-x", "deployment"), ("doc-x", "service")}

    # 자동 근거가 사라져도 사람이 쓴 링크는 남는다.
    world.restore_ingest("op-x")
    assert _links(world) == {("doc-x", "deployment"), ("doc-x", "service")}

    # 수동 삭제는 자동보다 우선한다. 다시 재생해도 되살아나지 않는다.
    _manual_edit(world, source_id, "op-m2", "# doc-x\n[[service]]\n")
    page = world._rebuild("resync", world.page_id("deployment"))
    database.apply_restored_wiki_state(WS, [page], {"removed_links": [], "restored_links": page["supported_links"]}, True)
    assert _links(world) == {("doc-x", "service")}

    # 소스를 수동 삭제 전 스냅샷으로 되돌리면 그 스냅샷까지의 수동 의도를 다시 재생한다.
    restored = world.restore.restore_source_page("restore-m2", "op-m1", WS, SourceSnapshotRestoreCommand(source_id, "doc-x"))
    database.apply_restored_wiki_state(WS, [restored], {"removed_links": [], "restored_links": []}, True)
    assert _links(world) == {("doc-x", "deployment"), ("doc-x", "service")}


def test_reingest_dropping_a_concept_unlinks_only_that_document(world: World) -> None:
    for document_id in ("doc-r", "doc-x"):
        _source(world, document_id)
    world.ingest("op-r", "doc-r", {"deployment": {"title": "Deployment", "evidence": ["B1"]}})
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "evidence": ["B2"]}})
    run_id = world._run("ingest", {})

    with world.connect() as conn:
        apply_structural_reconciliation(conn, [{
            "pipeline_run_id": run_id, "document_id": "doc-r", "workspace_id": WS, "structural_reconciled": False,
            "stale_concept_slugs": ["deployment"], "stale_relations": [],
        }], set())

    assert _links(world) == {("doc-x", "deployment")}


def test_manual_intent_survives_the_target_being_merged_away_and_merge_restore(world: World) -> None:
    for document_id in ("doc-x", "doc-d"):
        _source(world, document_id)
    doc_y = _source(world, "doc-y", "# doc-y\n")
    doc_m = _source(world, "doc-m", "# doc-m\n")
    world.ingest("op-x", "doc-x", {"deployment": {"title": "Deployment", "concept_refs": _refs(ROLLOUT),
                                                  "entry_changes": [dict(entry) for entry in ROLLOUT]}})
    world.ingest("op-d", "doc-d", {"deployment": {"title": "Deployment", "evidence": ["B99"]}})
    world.ingest("op-y", "doc-y", {"deployment": {"title": "Deployment", "concept_refs": ["B20"],
                                                  "entry_changes": [_add("e-history", "History", "e-rollout", ["B20"])]}})
    world.lint("lint-split")
    world.restore_ingest("op-x")
    # doc-y는 자동 링크를 사람이 지웠고, doc-m은 근거 없이 사람이 이었다.
    _manual_edit(world, doc_y, "op-y1", "# doc-y\n[[rollout]]\n")
    _manual_edit(world, doc_y, "op-y2", "# doc-y\n")
    _manual_edit(world, doc_m, "op-m1", "# doc-m\n[[rollout]]\n")
    assert {link for link in _links(world) if link[1] == "rollout"} == {("doc-m", "rollout")}

    world.lint("lint-merge")
    assert not [link for link in _links(world) if link[1] == "rollout"]

    world.restore_lint("lint-merge")

    # 대상이 다시 살아나면 수동 삭제는 계속 자동보다 우선하고, 수동 전용 링크는 돌아온다.
    assert {link for link in _links(world) if link[1] == "rollout"} == {("doc-m", "rollout")}
