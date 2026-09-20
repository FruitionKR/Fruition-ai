"""Exercise session ownership against PostgreSQL; CI supplies an isolated DB."""
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest

from app.modules.wiki_ingestion.infrastructure import workspace_concept_lock as locks

pytestmark = pytest.mark.skipif(
    not os.environ.get("CONCEPT_LOCK_TEST_DSN"), reason="CONCEPT_LOCK_TEST_DSN not set",
)


@pytest.fixture
def postgres_lock(monkeypatch):
    dsn = os.environ["CONCEPT_LOCK_TEST_DSN"]

    class FastTimeout:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, params=None):
            if query == "SET LOCAL lock_timeout = '60s'":
                query = "SET LOCAL lock_timeout = '250ms'"
            return self.connection.execute(query, params)

    @contextmanager
    def connect():
        with psycopg.connect(dsn) as connection:
            yield FastTimeout(connection)

    monkeypatch.setattr(locks, "_connect", connect)
    workspace = "lock-test-" + uuid4().hex

    def available():
        with psycopg.connect(dsn) as connection:
            return connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                ("wiki:concept-lock:" + workspace,),
            ).fetchone()[0]

    return workspace, available


def test_nested_finalization_retains_exclusion_until_outer_exit(postgres_lock):
    workspace, available = postgres_lock
    with locks.concept_write_lock(workspace, "run-1"):
        # This second acquisition previously timed out on a new DB session.
        with locks.concept_write_lock(workspace, "run-1"):
            assert not available()
        assert not available()
    assert available()


def test_same_run_on_another_worker_still_contends(postgres_lock):
    workspace, available = postgres_lock

    def worker():
        with locks.concept_write_lock(workspace, "run-1"):
            return True

    with ThreadPoolExecutor(max_workers=1) as executor:
        with locks.concept_write_lock(workspace, "run-1"):
            with pytest.raises(RuntimeError, match="acquisition timed out"):
                executor.submit(worker).result(timeout=5)
            assert not available()
        assert executor.submit(worker).result(timeout=5)
    assert available()


def test_different_run_does_not_reuse_outer_ownership(postgres_lock):
    workspace, available = postgres_lock
    with locks.concept_write_lock(workspace, "run-1"):
        with pytest.raises(RuntimeError, match="acquisition timed out"):
            with locks.concept_write_lock(workspace, "run-2"):
                pytest.fail("another run entered a locked workspace")
        with locks.concept_write_lock(workspace, "run-1"):
            assert not available()
    assert available()


def test_nested_failure_releases_lock_and_other_workspace_is_independent(postgres_lock):
    workspace, available = postgres_lock
    with pytest.raises(ValueError, match="finalization failed"):
        with locks.concept_write_lock(workspace, "run-1"):
            with locks.concept_write_lock(workspace + "-other", "run-1"):
                with locks.concept_write_lock(workspace, "run-1"):
                    raise ValueError("finalization failed")
    assert available()
    with locks.concept_write_lock(workspace, "run-1"):
        assert not available()
    assert available()
