"""재편입이 페이지를 다시 그릴 때, 원장 반영이 늦은 다른 문서 편입의 근거를 지우지 않는다(실제 PostgreSQL)."""
from __future__ import annotations

import pytest

from app.modules.wiki_ingestion.infrastructure import postgres_wiki_output_persistence as persistence
from app.modules.wiki_ingestion.infrastructure.pending_contributions import (
    record_pending_contributions,
    with_pending_contributions,
)
from tests.modules.wiki_ingestion.test_concept_entry_lint_postgres import (  # noqa: F401
    WS,
    World,
    database,
    pytestmark,
    world,
)


@pytest.fixture(autouse=True)
def _ledger_reader(world: World, monkeypatch) -> None:
    # World는 저장소 모듈의 원장·객체 접근만 바꾼다. 재편입 재생 모듈도 같은 가짜 원장·객체를 쓰게 한다.
    monkeypatch.setattr(persistence, "read_contributions", world.read_contributions)
    monkeypatch.setattr(persistence, "read_text_object", world.read)
    monkeypatch.setattr(persistence, "write_text_object", world.write)


def _lagged_ingest(world: World, operation_id: str, document_id: str, evidence: list[str]) -> str:
    """AI는 커밋했고 document-svc 원장에는 아직 오르지 않은 편입. 커밋이 남기는 대기 행만 둔다."""
    world.ingest(operation_id, document_id, {"pod": {"evidence": evidence}})
    world.ledger = [row for row in world.ledger if row["ingest_operation_id"] != operation_id]
    page_id = world.page_id("pod")
    with world.connect() as conn:
        record_pending_contributions(conn, WS, operation_id, document_id, [
            {"page_id": page_id, "contribution_key": f"wiki/{WS}/pages/{page_id}/ops/{operation_id}.json"},
        ])
    return page_id


def _reingest_b(world: World, page_id: str) -> list[str]:
    """문서 B 재편입(op-b2)이 B5를 고쳐 P를 다시 그린다. 이번 편입은 P에 새 기여가 없다."""
    deleted: list[str] = []
    with world.connect() as conn:
        conn.execute(
            "INSERT INTO document_wiki_links (document_id, wiki_page_id, relation_type, workspace_id, created_at) "
            "VALUES ('doc-b', %s, 'extracted_concept', %s, now()) ON CONFLICT DO NOTHING",
            (page_id, WS),
        )
        persistence._rebuild_invalidated_pages(
            conn, "doc-b", WS, "op-b2", ["doc-b:B5"], {"concept_contributions": {}, "concept_pages": []},
            {}, [], lambda *args: None, None, lambda _conn, _workspace_id, page_ids: deleted.extend(page_ids),
        )
    return deleted


def test_reingest_rebuild_keeps_committed_contribution_missing_from_ledger(world: World) -> None:
    world.ingest("op-b1", "doc-b", {"pod": {"evidence": ["B5"]}})
    page_id = _lagged_ingest(world, "op-a1", "doc-a", ["B1"])

    # 대기 기여가 없으면 P에 B의 무효화된 근거만 남은 것으로 보고 페이지를 지웠다.
    assert _reingest_b(world, page_id) == []
    markdown = world.objects[f"wiki/{WS}/concepts/pod.md"]
    assert "pod 근거 B5(doc-b)" not in markdown
    assert "pod 근거 B1(doc-a)" in markdown


def test_pending_row_is_dropped_once_ledger_has_it(world: World) -> None:
    world.ingest("op-b1", "doc-b", {"pod": {"evidence": ["B5"]}})
    page_id = _lagged_ingest(world, "op-a1", "doc-a", ["B1"])
    with world.connect() as conn:
        merged = with_pending_contributions(conn, world.read_contributions([page_id], WS), [page_id])
        assert [(row["ingest_operation_id"], row["sequence_revision"], row["active"]) for row in merged] == [
            ("op-b1", 1, True), ("op-a1", 2, True),
        ]
    # document-svc가 결과를 반영하면 원장 행만 쓰고 대기 행은 지운다.
    world._record(page_id, "op-a1", "doc-a")
    with world.connect() as conn:
        merged = with_pending_contributions(conn, world.read_contributions([page_id], WS), [page_id])
        assert [row["ingest_operation_id"] for row in merged] == ["op-b1", "op-a1"]
        assert conn.execute("SELECT count(*) AS n FROM wiki_pending_contributions").fetchone()["n"] == 0


def test_pending_contribution_of_trashed_document_is_inactive(world: World) -> None:
    world.ingest("op-b1", "doc-b", {"pod": {"evidence": ["B5"]}})
    page_id = _lagged_ingest(world, "op-a1", "doc-a", ["B1"])
    # 반영 전에 문서 A를 휴지통으로 옮기면 document-svc도 꺼진 행으로 넣는다.
    with world.connect() as conn:
        conn.execute(
            "INSERT INTO wiki_source_tombstones (workspace_id, document_id, deleted_at) "
            "VALUES (%s, 'doc-a', clock_timestamp() + interval '1 second')",
            (WS,),
        )
        merged = with_pending_contributions(conn, world.read_contributions([page_id], WS), [page_id])
    assert [(row["ingest_operation_id"], row["active"]) for row in merged] == [("op-b1", True), ("op-a1", False)]


def test_pending_rows_follow_page_purge(world: World) -> None:
    world.ingest("op-b1", "doc-b", {"pod": {"evidence": ["B5"]}})
    page_id = _lagged_ingest(world, "op-a1", "doc-a", ["B1"])
    # 페이지 삭제는 상태만 바꿔(되돌리기로 되살릴 수 있다) 대기 행을 남긴다. 워크스페이스 파기처럼 행을 지울 때만 함께 지워진다.
    with world.connect() as conn:
        conn.execute("DELETE FROM wiki_pages WHERE id = %s", (page_id,))
        assert conn.execute("SELECT count(*) AS n FROM wiki_pending_contributions").fetchone()["n"] == 0
