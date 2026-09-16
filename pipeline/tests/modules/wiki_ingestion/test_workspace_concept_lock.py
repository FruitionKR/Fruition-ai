from contextlib import contextmanager

import pytest
from psycopg.errors import LockNotAvailable

from app.modules.wiki_ingestion.infrastructure import workspace_concept_lock


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...] | None]] = []
        self.exit_exception = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, _traceback):
        self.exit_exception = exc_type
        return False

    def execute(self, query: str, params: tuple[str, ...] | None = None) -> None:
        self.calls.append((query, params))


def test_concept_lock_uses_workspace_advisory_key_and_releases_on_exception(monkeypatch) -> None:
    connection = FakeConnection()
    monkeypatch.setattr(workspace_concept_lock, "_connect", lambda: connection)

    with pytest.raises(RuntimeError, match="boom"):
        with workspace_concept_lock.concept_write_lock("workspace-1", "run-1"):
            raise RuntimeError("boom")

    assert connection.calls == [
        ("SET LOCAL lock_timeout = '60s'", None),
        (
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            ("wiki:concept-lock:workspace-1",),
        ),
        (
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            ("wiki:concept-lock:workspace-1",),
        ),
    ]
    assert connection.exit_exception is RuntimeError


def test_concept_lock_keys_differ_by_workspace(monkeypatch) -> None:
    connections: list[FakeConnection] = []

    @contextmanager
    def connect():
        connection = FakeConnection()
        connections.append(connection)
        yield connection

    monkeypatch.setattr(workspace_concept_lock, "_connect", connect)

    with workspace_concept_lock.concept_write_lock("workspace-1", "run-1"):
        pass
    with workspace_concept_lock.concept_write_lock("workspace-2", "run-2"):
        pass

    assert [connection.calls[1][1][0] for connection in connections] == [
        "wiki:concept-lock:workspace-1",
        "wiki:concept-lock:workspace-2",
    ]


def test_concept_lock_timeout_fails_without_unlock(monkeypatch) -> None:
    class FailingConnection(FakeConnection):
        def execute(self, query: str, params: tuple[str, ...] | None = None) -> None:
            super().execute(query, params)
            if query == "SELECT pg_advisory_lock(hashtextextended(%s, 0))":
                raise LockNotAvailable("lock timeout")

    connection = FailingConnection()
    monkeypatch.setattr(workspace_concept_lock, "_connect", lambda: connection)

    with pytest.raises(RuntimeError, match="acquisition timed out"):
        with workspace_concept_lock.concept_write_lock("workspace-1", "run-1"):
            pass

    assert connection.calls == [
        ("SET LOCAL lock_timeout = '60s'", None),
        (
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            ("wiki:concept-lock:workspace-1",),
        ),
    ]


def test_concept_index_cache_is_scoped_by_user_and_workspace(monkeypatch) -> None:
    calls: list[tuple] = []

    class Redis:
        def get(self, key):
            calls.append(("get", key))
            return None

        def setex(self, *args):
            calls.append(("setex", *args))

        def delete(self, key):
            calls.append(("delete", key))

    monkeypatch.setattr(workspace_concept_lock, "_client", lambda: Redis())

    workspace_concept_lock.get_concept_index("user-1", "workspace-1")
    workspace_concept_lock.put_concept_index("user-1", "workspace-1", [])
    workspace_concept_lock.invalidate_concept_index("user-1", "workspace-1")

    assert [call[1] for call in calls] == [
        "wiki:concept-index:user-1:workspace-1",
        "wiki:concept-index:user-1:workspace-1",
        "wiki:concept-index:user-1:workspace-1",
    ]
