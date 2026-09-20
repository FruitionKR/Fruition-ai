from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

import redis


INDEX_TTL_SECONDS = 300
_held_locks = threading.local()


@lru_cache(maxsize=1)
def _client() -> redis.Redis:
    url = os.environ.get("REDIS_URL")
    if url:
        return redis.Redis.from_url(url, decode_responses=True)
    return redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"),
                       port=int(os.environ.get("REDIS_PORT", "6379")),
                       username=os.environ.get("REDIS_USERNAME"),
                       password=os.environ.get("REDIS_PASSWORD"),
                       ssl=os.environ.get("REDIS_SSL", "false").lower() == "true",
                       decode_responses=True)


def _connect():
    import psycopg

    url = os.environ.get("AI_DATABASE_URL")
    if not url:
        raise RuntimeError("Set AI_DATABASE_URL before using the workspace concept lock")
    return psycopg.connect(url)


def _lock_key(workspace_id: str) -> str:
    return f"wiki:concept-lock:{workspace_id}"


@contextmanager
def concept_write_lock(workspace_id: str, run_id: str) -> Iterator[None]:
    from psycopg.errors import LockNotAvailable

    # Finalization and persistence both protect the same workspace. Reuse only
    # this synchronous execution's lock; another worker/run must still acquire
    # its own PostgreSQL lock, even when it receives the same run ID.
    if not hasattr(_held_locks, "scopes"):
        _held_locks.scopes = set()
    scopes = _held_locks.scopes
    scope = (os.environ.get("AI_DATABASE_URL"), workspace_id, run_id)
    if scope in scopes:
        yield
        return

    key = _lock_key(workspace_id)
    with _connect() as connection:
        try:
            connection.execute("SET LOCAL lock_timeout = '60s'")
            connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (key,),
            )
        except LockNotAvailable as exc:
            raise RuntimeError(
                f"workspace concept lock acquisition timed out: {workspace_id}"
            ) from exc
        scopes.add(scope)
        try:
            yield
        finally:
            try:
                connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (key,),
                )
            finally:
                scopes.remove(scope)


def get_concept_index(user_id: str, workspace_id: str) -> list[dict[str, Any]] | None:
    value = _client().get(f"wiki:concept-index:{user_id}:{workspace_id}")
    return json.loads(value) if value else None


def put_concept_index(
    user_id: str,
    workspace_id: str,
    concepts: list[dict[str, Any]],
) -> None:
    _client().setex(
        f"wiki:concept-index:{user_id}:{workspace_id}", INDEX_TTL_SECONDS,
        json.dumps(concepts, ensure_ascii=False),
    )


def invalidate_concept_index(user_id: str, workspace_id: str) -> None:
    _client().delete(f"wiki:concept-index:{user_id}:{workspace_id}")
