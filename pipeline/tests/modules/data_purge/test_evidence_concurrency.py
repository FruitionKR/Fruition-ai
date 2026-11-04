"""근거 본문을 비우는 동안 진행 중 run이 같은 행을 갱신해도 그 갱신이 사라지지 않는지 실제 PostgreSQL로 검증한다."""

import threading
import time

import pytest
from psycopg.types.json import Json

from app.modules.data_purge.infrastructure import document_copy_purge as purge
from tests.modules.agent_run.test_agent_run_postgres import (  # noqa: F401
    _workflow_run,
    repositories,
)
from tests.modules.agent_run.test_expired_artifact_cleanup import (  # noqa: F401
    cleanup_storage,
    storage,
)
from tests.modules.data_purge.test_document_copy_purge import (  # noqa: F401
    BODY,
    RUN_ID,
    _purge,
    _task_run,
    stack,
    wiki_objects,
)

PIPELINE_RUN_ID = "66666666-6666-6666-6666-666666666666"
EVIDENCE = {"evidence_snippets": [{"source_document_id": "doc-1", "text": BODY},
                                  {"source_document_id": "doc-2", "text": "다른 근거"}]}
# (테이블, 열, 키 열, 키)
AI_TASK = ("ai_task_runs", "result", "id", "query-1")
AGENT = ("agent_runs", "result", "id", RUN_ID)
PIPELINE = ("pipeline_runs", "manifest", "id", PIPELINE_RUN_ID)
REVIEW = ("document_quality_reviews", "evaluations", "document_id", "doc-2")
LOCK_WAIT_SECONDS = 10


def _insert_pipeline_run(conn, manifest) -> None:
    conn.execute("INSERT INTO pipeline_runs(id, document_id, workspace_id, input_source, output_dir, mode, status, manifest) "
                 "VALUES (%s, 'doc-2', 'ws', 'i', 'o', 'm', 'succeeded', %s)", (PIPELINE_RUN_ID, Json(manifest)))


def _seed(stack, target, value=EVIDENCE) -> None:  # noqa: F811
    runs, _, connect, _ = stack
    if target is AI_TASK:
        _task_run(connect, "query-1", "query", {"run_id": "query-1"}, value)
    elif target is AGENT:
        runs.create_with_planning_job(_workflow_run(), "edit-job")
        with connect() as conn:
            conn.execute("UPDATE agent_runs SET result = %s, status = 'completed' WHERE id = %s", (Json(value), RUN_ID))
    elif target is PIPELINE:
        with connect() as conn:
            _insert_pipeline_run(conn, value)
    else:
        with connect() as conn:
            _insert_pipeline_run(conn, {})
            conn.execute("INSERT INTO document_quality_reviews(document_id, workspace_id, ingest_run_id, evaluations) "
                         "VALUES ('doc-2', 'ws', %s, %s)", (PIPELINE_RUN_ID, Json([value])))


def _stored(connect, target):
    table, column, key, row = target
    with connect() as conn:
        return conn.execute(f"SELECT {column} AS value FROM {table} WHERE {key} = %s", (row,)).fetchone()["value"]


def _write_late(conn, target, sequence: int = 1) -> None:
    table, column, key, row = target
    conn.execute(f"UPDATE {table} SET {column} = {column} || %s::jsonb WHERE {key} = %s", (Json({"late": sequence}), row))


def _after_read(monkeypatch, action) -> None:
    """행을 읽은 뒤 비우기 전에 action을 끼워 넣는다. 재귀 호출이 아니라 근거를 담은 객체를 만났을 때만 부른다."""
    original = purge._blank_source_text

    def wrapped(value, document_id):
        if isinstance(value, dict) and "evidence_snippets" in value:
            action()
        return original(value, document_id)

    monkeypatch.setattr(purge, "_blank_source_text", wrapped)


@pytest.mark.parametrize("target", [AI_TASK, AGENT, PIPELINE, REVIEW], ids=lambda target: target[0])
def test_committed_update_between_read_and_write_survives(stack, monkeypatch, target) -> None:
    _, _, connect, _ = stack
    _seed(stack, target)
    fired = []

    def writer() -> None:
        if not fired:
            fired.append(True)
            with connect() as conn:
                _write_late(conn, target)

    _after_read(monkeypatch, writer)

    result = _purge()

    stored = str(_stored(connect, target))
    assert fired and result.finished
    assert "'late': 1" in stored and BODY not in stored and "다른 근거" in stored


def test_parallel_purge_of_another_document_does_not_restore_the_blanked_body(stack, monkeypatch) -> None:
    _, _, connect, _ = stack
    _seed(stack, AI_TASK)
    fired = []

    def other_purge() -> None:
        if not fired:
            fired.append(True)
            assert _purge("doc-2").finished

    _after_read(monkeypatch, other_purge)

    assert _purge("doc-1").finished

    stored = str(_stored(connect, AI_TASK))
    assert fired and BODY not in stored and "다른 근거" not in stored


def test_update_holding_the_row_lock_survives_after_the_purge_waits(stack, monkeypatch) -> None:
    _, _, connect, _ = stack
    _seed(stack, PIPELINE)
    threads = []

    def commit_once_purge_waits(conn) -> None:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        with connect() as observer:
            observer.autocommit = True
            while time.monotonic() < deadline and not observer.execute(
                "SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
            ).fetchone():
                time.sleep(0.02)
        conn.commit()
        conn.close()

    def writer() -> None:
        if not threads:
            conn = connect()
            _write_late(conn, PIPELINE)
            threads.append(threading.Thread(target=commit_once_purge_waits, args=(conn,)))
            threads[0].start()

    _after_read(monkeypatch, writer)

    result = _purge()
    threads[0].join()

    stored = str(_stored(connect, PIPELINE))
    assert result.finished and "'late': 1" in stored and BODY not in stored


def test_retries_are_bounded_and_the_next_cycle_finishes(stack, monkeypatch) -> None:
    _, _, connect, _ = stack
    _seed(stack, PIPELINE)
    with connect() as conn:
        conn.execute("INSERT INTO source_blocks(document_id, block_id, text) VALUES ('doc-1', 'b', %s)", (BODY,))
    writes = []
    busy = [True]

    def writer() -> None:
        if busy[0]:
            writes.append(True)
            with connect() as conn:
                _write_late(conn, PIPELINE, len(writes))

    _after_read(monkeypatch, writer)

    result = _purge()  # 예외를 올리면 consumer offset이 멈춰 파티션이 막힌다.

    assert len(writes) == purge._EVIDENCE_UPDATE_ATTEMPTS
    assert result.pending_evidence == 1 and not result.failed and not result.finished
    with connect() as conn:
        assert conn.execute("SELECT finished_at FROM ai_document_purges").fetchone()["finished_at"] is None
        # 다른 행의 삭제까지 되돌리지 않는다.
        assert conn.execute("SELECT count(*) AS c FROM source_blocks").fetchone()["c"] == 0

    busy[0] = False
    retried = purge.purge_pending_documents()

    stored = str(_stored(connect, PIPELINE))
    assert retried.finished == 1 and BODY not in stored and f"'late': {len(writes)}" in stored
    with connect() as conn:
        assert conn.execute("SELECT finished_at FROM ai_document_purges").fetchone()["finished_at"] is not None


def test_row_deleted_while_retrying_does_not_leave_the_purge_pending(stack, monkeypatch) -> None:
    _, _, connect, _ = stack
    _seed(stack, AI_TASK)
    fired = []

    def writer() -> None:
        if not fired:
            fired.append(True)
            with connect() as conn:
                conn.execute("DELETE FROM ai_task_runs WHERE id = 'query-1'")

    _after_read(monkeypatch, writer)

    assert _purge().finished


def test_redelivery_does_not_rewrite_the_blanked_row(stack) -> None:
    _, _, connect, _ = stack
    _seed(stack, PIPELINE)
    _purge()
    with connect() as conn:
        before = conn.execute("SELECT xmin::text AS version, manifest FROM pipeline_runs WHERE id = %s",
                              (PIPELINE_RUN_ID,)).fetchone()

    result = _purge()

    with connect() as conn:
        after = conn.execute("SELECT xmin::text AS version, manifest FROM pipeline_runs WHERE id = %s",
                             (PIPELINE_RUN_ID,)).fetchone()
    assert result.finished and after == before and BODY not in str(after["manifest"])


def test_number_python_cannot_round_trip_does_not_exhaust_retries(stack) -> None:
    _, _, connect, _ = stack
    _seed(stack, PIPELINE)
    with connect() as conn:
        conn.execute("UPDATE pipeline_runs SET manifest = manifest || '{\"score\": 0.12345678901234567890123}'::jsonb "
                     "WHERE id = %s", (PIPELINE_RUN_ID,))

    result = _purge()

    assert result.finished and BODY not in str(_stored(connect, PIPELINE))
