"""하위 항목 단위 검색(ADR 0042 4단계)을 실제 PostgreSQL로 확인한다.

질의는 `sub_entry` 단위와 `entry_id`가 붙은 근거 단위에 걸리고, 그 단위는 "페이지 제목 > 상위 항목 > 항목" 경로를 함께
읽는다.
"""
from __future__ import annotations

from app.modules.query.infrastructure.postgres_wiki_repository import PostgresWikiRepository
from tests.modules.wiki_ingestion.test_concept_entry_lint_postgres import (  # noqa: F401
    WS,
    World,
    _add,
    pytestmark,
    world,
)


def _deployment(world: World) -> None:
    world.ingest("op-x", "doc-x", {
        "deployment": {"title": "Deployment", "entry_changes": [
            _add("e-rollout", "Rollout", refs=["B0001"]),
            _add("e-history", "History", "e-rollout", ["B0002"]),
        ]},
    })


def test_query_hits_sub_entry_and_its_evidence_with_entry_path(world: World) -> None:
    _deployment(world)
    repository = PostgresWikiRepository()

    pages = repository.list_candidate_pages(WS, "History", source_limit=5, concept_limit=5)
    [page_id] = [page.id for page in pages]
    units = repository.list_embedding_units_by_page_ids([page_id])[page_id]
    workspace_units = {unit.id: unit for unit, _title in repository.list_workspace_embedding_units(WS)}

    by_text = {unit.text: unit.entry_path for unit in units}
    assert by_text["Rollout > History"] == "Deployment > Rollout > History"
    assert by_text["Deployment > Rollout"] == "Deployment > Rollout"
    # 항목의 근거 블록에만 걸린 근거 단위도 그 항목 경로를 가진다.
    assert by_text["History 근거(doc-x)"] == "Deployment > Rollout > History"
    assert {unit.id: unit.entry_path for unit in units} == {
        unit_id: unit.entry_path for unit_id, unit in workspace_units.items()
    }

