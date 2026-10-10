import os
from unittest.mock import Mock
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.rows import dict_row

from app.modules.model_usage.infrastructure import usage_ledger as ledger
from app.modules.wiki_ingestion.infrastructure import migrate_ai_schema
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as repository


def test_unset_converter_role_skips_grant(monkeypatch):
    monkeypatch.delenv("AI_DB_CONVERTER_ROLE", raising=False)
    connection = Mock()
    migrate_ai_schema.grant_converter_access(connection)
    connection.execute.assert_not_called()


@pytest.fixture
def converter_db(monkeypatch):
    """격리 schema와 NOLOGIN role로 converter 계정을 흉내 낸다. role은 SET ROLE로만 쓴다."""
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    name = "test_converter_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(name)))
        # 플랫폼 init-db-isolation.sh가 converter role에 주는 schema USAGE와 같다.
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(name), sql.Identifier(name)))
    separator = "&" if "?" in dsn else "?"
    scoped = f"{dsn}{separator}options={quote(f'-csearch_path={name}')}"
    monkeypatch.setenv("AI_DB_MIGRATION_URL", scoped)
    monkeypatch.setenv("AI_DATABASE_URL", scoped)
    monkeypatch.setenv("AI_DB_CONVERTER_ROLE", name)

    def connect_as_converter():
        conn = psycopg.connect(scoped, row_factory=dict_row)
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(name)))
        return conn

    try:
        yield name, scoped, connect_as_converter
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def test_schema_bootstrap_grants_only_ledger_privileges(converter_db, monkeypatch):
    role, scoped, connect_as_converter = converter_db
    # migration Job과 기동 부트스트랩 두 경로를 차례로 적용해 반복 적용도 확인한다.
    migrate_ai_schema.main()
    repository.ensure_ai_schema()

    with psycopg.connect(scoped) as conn:
        granted = conn.execute(
            """SELECT privilege_type FROM information_schema.role_table_grants
               WHERE grantee = %s ORDER BY privilege_type""", (role,)).fetchall()
        columns = conn.execute(
            """SELECT column_name FROM information_schema.column_privileges
               WHERE grantee = %s AND privilege_type = 'SELECT' ORDER BY column_name""", (role,)).fetchall()
    assert [row[0] for row in granted] == ["INSERT", "UPDATE"]
    assert [row[0] for row in columns] == ["finished_at", "id", "status"]

    monkeypatch.setattr(ledger.database, "connect_ai", connect_as_converter)
    with ledger.usage_scope({"run_id": "r", "workspace_id": "w", "user_id": "u", "kind": "conversion"}):
        call_id = ledger.start_call("openai", "model")
    ledger.finish_call(call_id, "succeeded", {"usage": {"input_tokens": 3, "output_tokens": 2}}, "model", 1.0)
    with psycopg.connect(scoped, row_factory=dict_row) as conn:
        row = conn.execute("SELECT status, input_tokens, finished_at FROM ai_model_usage WHERE id = %s",
                           (call_id,)).fetchone()
    assert row["status"] == "succeeded" and row["input_tokens"] == 3 and row["finished_at"] is not None

    for statement in ("SELECT * FROM ai_model_usage", "DELETE FROM ai_model_usage", "SELECT id FROM ai_task_runs"):
        with connect_as_converter() as conn, pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)
