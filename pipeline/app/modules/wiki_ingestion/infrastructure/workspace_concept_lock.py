from __future__ import annotations

import json
import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

import redis


INDEX_TTL_SECONDS = 300


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

    del run_id
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
        try:
            yield
        finally:
            connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (key,),
            )


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
