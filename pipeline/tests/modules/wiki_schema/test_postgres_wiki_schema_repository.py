"""위키 스키마 활성화가 요청한 workspace·user 범위 안에서만 동작하는지 실제 PostgreSQL로 확인한다. CI는 격리 DB를 준다."""
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.modules.wiki_schema.infrastructure import postgres_wiki_schema_repository as repository_module
from app.modules.wiki_schema.infrastructure.postgres_wiki_schema_repository import PostgresWikiSchemaRepository

DSN = os.environ.get("TEST_AGENT_DATABASE_URL") or os.environ.get("CONCEPT_LOCK_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")

AI_SCHEMA_SQL = (Path(__file__).resolve().parents[3] / "db" / "ai_schema.sql").read_text(encoding="utf-8")


@pytest.fixture
def connect(monkeypatch):
    name = "test_wiki_schema_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))

    def _connect():
        return psycopg.connect(DSN, row_factory=dict_row, options=f"-csearch_path={name}")

    try:
        with _connect() as conn:
            conn.execute(AI_SCHEMA_SQL)
            conn.execute(
                "INSERT INTO wiki_schemas(id, workspace_id, user_id, name, raw_markdown, status) VALUES "
                "('victim-active', 'ws-victim', 'alice', 's', '', 'active'), "
                "('victim-draft', 'ws-victim', 'alice', 's', '', 'draft'), "
                "('attacker-draft', 'ws-attacker', 'mallory', 's', '', 'draft')"
            )
        monkeypatch.setattr(repository_module.database, "connect_ai", _connect)
        yield _connect
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _statuses(connect) -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT id, status FROM wiki_schemas").fetchall()
    return {row["id"]: row["status"] for row in rows}


@pytest.mark.parametrize("workspace_id, user_id", [("ws-attacker", "alice"), ("ws-victim", "mallory"), ("ws-attacker", "mallory")])
def test_activate_outside_requested_scope_is_not_found_and_changes_nothing(connect, workspace_id, user_id):
    before = _statuses(connect)

    with pytest.raises(ValueError, match="Schema not found."):
        PostgresWikiSchemaRepository().activate("victim-draft", workspace_id, user_id)

    assert _statuses(connect) == before
    assert before == {"victim-active": "active", "victim-draft": "draft", "attacker-draft": "draft"}


def test_activate_within_requested_scope_swaps_active_schema_of_that_scope_only(connect):
    record = PostgresWikiSchemaRepository().activate("victim-draft", "ws-victim", "alice")

    assert record.id == "victim-draft"
    assert record.status == "active"
    assert record.activated_at is not None
    assert _statuses(connect) == {"victim-active": "draft", "victim-draft": "active", "attacker-draft": "draft"}
