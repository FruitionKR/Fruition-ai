"""채팅 제목 호출을 document-svc 수집이 놓치지 않는지 실제 원장으로 본다."""
import os
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.modules.model_usage.infrastructure import usage_ledger as ledger
from app.workers import task_worker


def _command(run_id: str) -> dict:
    return {"kind": "agent", "run_id": run_id, "workspace_id": "w", "user_id": "u", "session_id": "s",
            "provider": "openai", "model": "gpt-6-luna", "message": "질문"}


def _document_keeps_collecting(calls: list[dict]) -> bool:
    # document-svc UsageChargeService.collectRun: started 행이 있으면 대기열에 남겨 다시 수집한다.
    return any(call["status"] == "started" for call in calls)


@pytest.fixture
def repository(monkeypatch):
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    name = "test_title_call_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    separator = "&" if "?" in dsn else "?"
    scoped = f"{dsn}{separator}options={quote(f'-csearch_path={name}')}"
    monkeypatch.setenv("AI_DATABASE_URL", scoped)
    monkeypatch.setenv("AI_DB_MIGRATION_URL", scoped)
    task_worker.database.ensure_ai_schema()
    try:
        yield ledger.PostgresUsageRepository()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def test_title_call_is_visible_as_started_before_publish_and_closes_on_one_row(repository) -> None:
    command = _command("run-" + uuid4().hex)

    reserved = task_worker._reserve_session_title_call(command, {"message": "답변"})

    calls = repository.list_calls(command["run_id"], None, None)
    assert [(call["kind"], call["status"]) for call in calls] == [("session_title", "started")]
    assert _document_keeps_collecting(calls)

    with ledger.usage_scope({**command, "kind": "session_title"}), ledger.reserved_call(reserved):
        with ledger.track_call("openai", "gpt-6-luna") as receipt:
            receipt["usage"] = {"input_tokens": 10, "output_tokens": 3}
    ledger.release_reserved_call(reserved)

    calls = repository.list_calls(command["run_id"], None, None)
    assert [(str(call["id"]), call["status"], call["input_tokens"]) for call in calls] == [(reserved, "succeeded", 10)]
    assert not _document_keeps_collecting(calls)


def test_reservation_without_model_call_leaves_no_row(repository) -> None:
    command = _command("run-" + uuid4().hex)

    reserved = task_worker._reserve_session_title_call(command, {"message": "답변"})
    ledger.release_reserved_call(reserved)

    assert repository.list_calls(command["run_id"], None, None) == []
