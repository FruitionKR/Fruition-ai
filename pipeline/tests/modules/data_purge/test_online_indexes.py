"""작업 기록 표의 인덱스를 스키마 트랜잭션 밖에서 잠금 없이 만드는지 실제 PostgreSQL로 검증한다."""

import os
from pathlib import Path
import threading
import time
from urllib.parse import quote
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from app.modules.wiki_ingestion.infrastructure import migrate_ai_schema as migrate
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as repository

SCHEMA_SQL = Path(__file__).resolve().parents[3].joinpath("db/ai_schema.sql").read_text(encoding="utf-8")
NAMES = sorted(migrate.ONLINE_INDEXES)


@pytest.fixture
def scoped(monkeypatch):
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    name = "test_online_index_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    url = f"{dsn}{'&' if '?' in dsn else '?'}options={quote(f'-csearch_path={name}')}"
    monkeypatch.setenv("AI_DB_MIGRATION_URL", url)
    monkeypatch.setenv("AI_DATABASE_URL", url)
    monkeypatch.delenv("AI_DB_CONVERTER_ROLE", raising=False)
    try:
        yield url
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _apply_schema(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(SCHEMA_SQL)


def _validity(url: str) -> dict[str, bool]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relnamespace = current_schema()::regnamespace AND c.relname = ANY(%s)",
            (NAMES,),
        ).fetchall()
    return dict(rows)


def test_schema_transaction_does_not_build_the_task_run_indexes(scoped) -> None:
    """이 트랜잭션은 앞선 ALTER TABLE의 ACCESS EXCLUSIVE를 끝까지 쥔다. 그 안에서 큰 표를 훑으면 안 된다."""
    with psycopg.connect(scoped) as conn:
        conn.execute(SCHEMA_SQL)
        locks = conn.execute(
            "SELECT mode FROM pg_locks WHERE pid = pg_backend_pid() AND relation = 'ai_task_runs'::regclass"
        ).fetchall()

    assert ("AccessExclusiveLock",) in locks  # 그래서 인덱스를 밖으로 뺐다. 이 전제가 바뀌면 다시 본다.
    assert _validity(scoped) == {}


def test_migration_builds_valid_indexes_and_is_repeatable(scoped) -> None:
    migrate.main()
    migrate.main()

    assert _validity(scoped) == {name: True for name in NAMES}
    with psycopg.connect(scoped) as conn:
        definition = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_ai_task_runs_command_document' "
            "AND schemaname = current_schema()"
        ).fetchone()[0]
    assert "document_id" in definition and "WHERE" in definition


def test_invalid_index_left_by_an_interrupted_build_is_rebuilt(scoped) -> None:
    migrate.main()
    with psycopg.connect(scoped) as conn:
        conn.execute("UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'idx_ai_task_runs_workspace_status'::regclass")

    assert migrate.create_online_indexes(scoped) == []

    assert _validity(scoped) == {name: True for name in NAMES}


def test_build_blocked_by_an_open_transaction_gives_up_and_the_next_run_recovers(scoped, monkeypatch) -> None:
    monkeypatch.setattr(migrate, "_ONLINE_INDEX_LOCK_TIMEOUT", "200ms")
    _apply_schema(scoped)
    with psycopg.connect(scoped) as holder:
        holder.execute("INSERT INTO ai_task_runs(id, workspace_id, user_id, kind) VALUES ('held', 'ws', 'u', 'query')")

        assert migrate.create_online_indexes(scoped) == NAMES  # 인덱스가 없어도 삭제는 동작하므로 예외를 올리지 않는다.

        assert True not in _validity(scoped).values()
        holder.rollback()

    assert migrate.create_online_indexes(scoped) == []  # 남은 잔해를 지우고 다시 만든다.
    assert _validity(scoped) == {name: True for name in NAMES}


def test_build_over_its_time_limit_gives_up_and_the_next_run_recovers(scoped, monkeypatch) -> None:
    monkeypatch.setattr(migrate, "_ONLINE_INDEX_BUILD_TIMEOUT", "1ms")
    _apply_schema(scoped)
    with psycopg.connect(scoped) as conn:
        conn.execute("INSERT INTO ai_task_runs(id, workspace_id, user_id, kind) "
                     "SELECT g::text, 'ws', 'u', 'query' FROM generate_series(1, 200000) g")

    assert migrate.create_online_indexes(scoped) == NAMES

    monkeypatch.setattr(migrate, "_ONLINE_INDEX_BUILD_TIMEOUT", "60s")
    assert migrate.create_online_indexes(scoped) == []


def test_migration_finishes_and_names_the_indexes_it_could_not_build(scoped, monkeypatch, capsys) -> None:
    monkeypatch.setattr(migrate, "create_online_indexes", lambda url: NAMES)

    migrate.main()

    output = capsys.readouterr().out
    assert all(name in output for name in NAMES) and "migration 완료" in output


def test_build_is_skipped_while_another_instance_changes_the_schema(scoped, monkeypatch) -> None:
    monkeypatch.setattr(migrate, "_ONLINE_INDEX_LOCK_TIMEOUT", "200ms")
    _apply_schema(scoped)
    with psycopg.connect(scoped, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (migrate.SCHEMA_CHANGE_LOCK,))

        assert migrate.create_online_indexes(scoped) == NAMES

    assert _validity(scoped) == {}


def test_starting_instance_waits_for_a_build_without_queuing_on_the_table(scoped) -> None:
    """표 잠금 대기열에 서면 그 뒤의 읽기가 모두 막힌다. 빌드 중에는 advisory lock에서만 기다려야 한다."""
    migrate.main()
    errors: list[BaseException] = []

    def start_instance() -> None:
        try:
            repository.ensure_ai_schema()
        except BaseException as exc:  # noqa: BLE001 - 테스트 스레드 실패를 본문에서 확인한다.
            errors.append(exc)

    with psycopg.connect(scoped, autocommit=True) as builder, psycopg.connect(scoped, autocommit=True) as reader:
        builder.execute("SELECT pg_advisory_lock(%s)", (migrate.SCHEMA_CHANGE_LOCK,))
        thread = threading.Thread(target=start_instance, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 30
            while not reader.execute(
                "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND objid = %s",
                (migrate.SCHEMA_CHANGE_LOCK,),
            ).fetchone():
                assert time.monotonic() < deadline, "기동 경로가 advisory lock을 기다리지 않는다."
                time.sleep(0.05)

            reader.execute("SET lock_timeout = '1s'")
            reader.execute("SELECT count(*) FROM ai_task_runs")
            assert reader.execute(
                "SELECT count(*) FROM pg_locks WHERE relation = 'ai_task_runs'::regclass AND pid <> pg_backend_pid()"
            ).fetchone()[0] == 0
        finally:
            builder.execute("SELECT pg_advisory_unlock(%s)", (migrate.SCHEMA_CHANGE_LOCK,))
        thread.join(30)

    assert not thread.is_alive() and errors == []
