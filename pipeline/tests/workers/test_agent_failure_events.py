"""Real route -> durable PostgreSQL state -> Kafka failure/redelivery contract."""
from contextlib import contextmanager
from unittest.mock import Mock

import psycopg
from psycopg.rows import dict_row
import pytest

from app.core.pipeline_control import PipelineRunCancelledError
from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.entities import AgentTurnRoute
from app.workers import task_worker
from tests.test_analytics import db  # isolated schema with the real migration
from tests.workers.test_task_worker import _run_consume


@pytest.fixture
def command():
    return dict(kind="agent", run_id="turn", workspace_id="ws", user_id="user",
                message="test question", provider="openai", model="gpt-5-nano",
                skill_mode="off", message_context={"user_message_id": "message-1"})


@pytest.fixture
def connection(db, monkeypatch):
    def connect():
        return psycopg.connect(db, row_factory=dict_row)
    monkeypatch.setattr(task_worker.database, "connect_ai", connect)
    return connect


def install_use_case(monkeypatch, *, action="chat_answer", error=None):
    router = Mock()
    router.route.return_value = AgentTurnRoute(action, 1.0, "test route")
    query = Mock()
    query.execute.side_effect = error or RuntimeError("private upstream details")
    def build(**kwargs):
        return HandleAgentTurnUseCase(
            router=router, query_use_case=query,
            markdown_edit_use_case=Mock(), markdown_create_use_case=Mock(),
            # No authorer is installed: skill_authoring fails at configuration validation.
            on_chat_answer=kwargs["on_chat_answer"],
        )
    monkeypatch.setattr(task_worker, "build_handle_agent_turn_use_case", build)
    return query


def consume(command):
    events = []
    _run_consume([lambda command, publisher: task_worker._handle_agent(command)],
                 "agent", command=command, emitted=events)
    assert len(events) == 1
    return events[0]


def test_failed_chat_answer_and_redelivery_preserve_original_envelope(connection, monkeypatch, command):
    query = install_use_case(monkeypatch)
    first = consume(command)
    replay = consume(command)
    for event in (first, replay):
        assert event["run_id"] == "turn" and event["status"] == "failed"
        assert event["payload"] == {"action": "chat_answer"}
        assert event["request"] == command
        assert event["request"]["message_context"] == {"user_message_id": "message-1"}
        assert event["error"] and "private upstream" not in event["error"]
    query.execute.assert_called_once()
    assert first["event_id"] == replay["event_id"]
    with connection() as conn:
        row = conn.execute("SELECT result, status FROM agent_runs WHERE id = 'turn'").fetchone()
    assert row["status"] == "failed" and row["result"]["action"] == "chat_answer"
    assert task_worker._agent_failed_payload({**command, "user_id": "other"}) is None


@pytest.mark.parametrize("action", ["markdown_edit", "skill_authoring"])
def test_nonquery_failure_never_acquires_chat_marker(connection, monkeypatch, command, action):
    query = install_use_case(monkeypatch, action=action)
    if action == "markdown_edit":
        # A concrete edit request reaches the edit path, whose generator fails.
        edits = []
        original = task_worker.build_handle_agent_turn_use_case
        def build(**kwargs):
            use_case = original(**kwargs)
            use_case._router.route.return_value = AgentTurnRoute(
                "markdown_edit", 1.0, "test", edit_goal="rewrite", edit_operation="replace",
                document_operation="edit", persist=True,
            )
            use_case._markdown_edit_use_case.execute.side_effect = RuntimeError("edit failed")
            edits.append(use_case._markdown_edit_use_case)
            return use_case
        monkeypatch.setattr(task_worker, "build_handle_agent_turn_use_case", build)
        command.update(document_id="doc", base_version=1,
                       editor_snapshot={"markdown": "existing text", "target": {
                           "type": "whole_document", "start_line": 1, "end_line": 1}})
    event = consume(command)
    assert event["status"] == "failed" and event["payload"] is None
    if action == "markdown_edit":
        edits[0].execute.assert_called_once()
    assert consume(command)["payload"] is None
    query.execute.assert_not_called()


def test_cancelled_query_is_excluded(connection, monkeypatch, command):
    query = install_use_case(monkeypatch)
    def cancel(*args, **kwargs):
        with connection() as conn:
            conn.execute("UPDATE agent_runs SET status = 'cancelled' WHERE id = 'turn'")
        raise PipelineRunCancelledError("cancelled")
    query.execute.side_effect = cancel
    event = consume(command)
    assert event["error"] == "agent_turn_cancelled" and event["payload"] is None
    assert consume(command)["payload"] is None


def test_interrupted_query_restores_marker_on_redelivery(connection, command):
    assert task_worker._register_agent_command(command) == ("execute", None)
    task_worker._record_agent_chat_answer("turn")
    # Simulate process death after route persistence, before a result is stored.
    event = consume(command)
    assert event["status"] == "failed" and event["payload"] == {"action": "chat_answer"}
    assert consume(command)["payload"] == {"action": "chat_answer"}


@pytest.fixture
def reject_optional_write(connection):
    # Fail the actual metadata UPDATE in PostgreSQL; product journal writes still work.
    with connection() as conn:
        conn.execute("""
            CREATE FUNCTION reject_analytics_marker() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.status = 'executing' AND NEW.result->>'action' = 'chat_answer' THEN
                    RAISE EXCEPTION 'private metadata write details';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER reject_analytics_marker BEFORE UPDATE ON agent_runs
            FOR EACH ROW EXECUTE FUNCTION reject_analytics_marker();
        """)


def test_optional_write_failure_keeps_successful_query(
    connection, reject_optional_write, monkeypatch, command, caplog,
):
    from tests.modules.agent.test_handle_agent_turn import FakeQueryUseCase
    query = install_use_case(monkeypatch)
    query.execute.side_effect = FakeQueryUseCase().execute
    events = []
    consumer, statuses, _ = _run_consume(
        [lambda command, publisher: task_worker._handle_agent(command)],
        "agent", command=command, emitted=events,
    )
    assert statuses[0] == "succeeded"
    assert events[0]["payload"]["action"] == "chat_answer"
    query.execute.assert_called_once()
    consumer.commit.assert_awaited_once()
    with connection() as conn:
        assert conn.execute("SELECT status FROM agent_runs WHERE id = 'turn'").fetchone()["status"] == "completed"
    assert "analytics_agent_classification_write_failed" in caplog.text
    assert "private metadata write details" not in caplog.text


def test_optional_write_failure_never_fabricates_failed_chat_marker(
    connection, reject_optional_write, monkeypatch, command, caplog,
):
    query = install_use_case(monkeypatch)
    assert consume(command)["payload"] is None
    assert consume(command)["payload"] is None
    query.execute.assert_called_once()
    assert "analytics_agent_classification_write_failed" in caplog.text
    assert "private metadata write details" not in caplog.text


def test_optional_read_failure_still_publishes_generic_failed_envelope(
    connection, monkeypatch, command, caplog,
):
    query = install_use_case(monkeypatch)

    class MetadataReadFailure:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("SELECT result FROM agent_runs"):
                raise psycopg.OperationalError("private classification read details")
            return self.conn.execute(sql, *args, **kwargs)

    @contextmanager
    def connect():
        with connection() as conn:
            yield MetadataReadFailure(conn)

    monkeypatch.setattr(task_worker.database, "connect_ai", connect)
    for expected_error in ("agent_turn_failed", "invalid_agent_turn_request"):
        event = consume(command)
        assert event["status"] == "failed" and event["payload"] is None
        assert event["run_id"] == "turn" and event["request"] == command
        assert event["error"] == expected_error
    query.execute.assert_called_once()
    assert "analytics_agent_classification_read_failed" in caplog.text
    assert "private classification read details" not in caplog.text
