"""하위 항목 단위 검색(ADR 0042 4단계)을 실제 PostgreSQL로 확인한다.

질의는 `sub_entry` 단위와 `entry_id`가 붙은 근거 단위에 걸리고, 그 단위는 "페이지 제목 > 상위 항목 > 항목" 경로를 함께
읽는다. 같은 하위 항목 찾기는 저장된 `sub_entry` 벡터를 쓴다.
"""
from __future__ import annotations

from app.modules.query.infrastructure.postgres_wiki_repository import PostgresWikiRepository
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database
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


def test_stored_sub_entry_vectors_are_loaded_by_entry_id_and_pending_ones_are_left_out(world: World) -> None:
    _deployment(world)
    with world.connect() as conn:
        conn.execute(
            """
            UPDATE wiki_embedding_vectors vector
            SET status = 'completed', embedding_vector = ARRAY[0.6, 0.8], embedding_dimension = 2
            FROM wiki_embedding_units unit
            WHERE unit.embedding_vector_id = vector.id AND unit.entry_id = 'e-history' AND unit.unit_type = 'sub_entry'
            """
        )
        model = conn.execute("SELECT DISTINCT embedding_model FROM wiki_embedding_vectors").fetchone()["embedding_model"]

    vectors = database.list_sub_entry_vectors(WS, model)

    assert list(vectors) == ["e-history"]
    assert [round(value, 3) for value in vectors["e-history"]] == [0.6, 0.8]
    assert database.list_sub_entry_vectors(WS, "other-model") == {}
