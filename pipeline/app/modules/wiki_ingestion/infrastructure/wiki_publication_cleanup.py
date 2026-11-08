from __future__ import annotations

import os
from datetime import timedelta

from app.core.pipeline_control import ScopePurgedError
from app.modules.wiki_ingestion.infrastructure.object_storage import delete_object
from app.modules.wiki_ingestion.infrastructure.wiki_publication import (
    _lock_publication,
    _lock_scope_writes,
    _reject_purged,
    connect,
)
from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import concept_write_lock


GRACE_SECONDS_ENV = "AI_WIKI_PUBLICATION_CLEANUP_GRACE_SECONDS"


_RETAINED_REFERENCE_SQL = """
EXISTS (
    SELECT 1 FROM wiki_pages
    WHERE markdown_uri = %(key)s
       OR right(markdown_uri, length(%(key)s) + 1) = '/' || %(key)s
)
OR EXISTS (
    SELECT 1 FROM wiki_pending_contributions WHERE object_key = %(key)s
)
OR EXISTS (
    SELECT 1 FROM wiki_publications
    WHERE state = 'COMMITTED'
      AND position(%(key)s in COALESCE(result::text, '')) > 0
)
OR EXISTS (
    SELECT 1 FROM pipeline_runs
    WHERE position(%(key)s in COALESCE(manifest::text, '')) > 0
)
OR EXISTS (
    SELECT 1 FROM ai_task_runs
    WHERE position(%(key)s in COALESCE(command::text, '')) > 0
       OR position(%(key)s in COALESCE(result::text, '')) > 0
)
OR EXISTS (
    SELECT 1 FROM ai_task_changes
    WHERE NOT undone
      AND (position(%(key)s in COALESCE(before_value::text, '')) > 0
        OR position(%(key)s in COALESCE(after_value::text, '')) > 0)
)
"""


def cleanup_configured_publications(limit: int = 20) -> int:
    raw = os.environ.get(GRACE_SECONDS_ENV)
    if raw is None:
        return 0
    try:
        grace_seconds = int(raw)
    except ValueError as exc:
        raise ValueError(f"{GRACE_SECONDS_ENV} must be a non-negative integer") from exc
    return cleanup_abandoned_publications(grace_seconds=grace_seconds, limit=limit)


def cleanup_abandoned_publications(*, grace_seconds: int, limit: int = 20) -> int:
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be non-negative")
    if limit <= 0:
        raise ValueError("limit must be positive")
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT publication_id, workspace_id
            FROM wiki_publications publication
            WHERE (
                publication.state = 'ABANDONED'
                AND publication.abandoned_at <= clock_timestamp() - %s
              OR publication.state = 'PREPARED'
                AND publication.prepared_at <= clock_timestamp() - %s
                AND NOT EXISTS (
                    SELECT 1 FROM ai_task_runs task
                    WHERE task.workspace_id = publication.workspace_id
                      AND task.user_id = publication.user_id
                      AND task.command->>'operation_id' = publication.operation_id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM pipeline_runs run
                    WHERE run.workspace_id = publication.workspace_id
                      AND run.status = 'running'
                      AND run.manifest->>'operation_id' = publication.operation_id
                )
            )
              AND EXISTS (
                  SELECT 1 FROM wiki_publication_objects object
                  WHERE object.publication_id = publication.publication_id
                    AND object.deleted_at IS NULL
                    AND NOT ({_RETAINED_REFERENCE_SQL.replace("%(key)s", "object.object_key")})
              )
            ORDER BY COALESCE(abandoned_at, prepared_at), publication_id
            LIMIT %s
            """,
            (timedelta(seconds=grace_seconds), timedelta(seconds=grace_seconds), limit),
        ).fetchall()
    deleted = 0
    for candidate in rows:
        try:
            deleted += _cleanup_publication(
                str(candidate["publication_id"]),
                str(candidate["workspace_id"]),
                grace_seconds,
            )
        except ScopePurgedError:
            # Purge owns object removal after its fence; cleanup must not recreate its rows or bytes.
            continue
    return deleted


def _cleanup_publication(identifier: str, workspace_id: str, grace_seconds: int) -> int:
    deleted = 0
    with concept_write_lock(workspace_id, identifier):
        with connect() as conn:
            _lock_publication(conn, identifier)
            publication = conn.execute(
                """
                SELECT operation_id, workspace_id, user_id, state, prepared_at, abandoned_at
                FROM wiki_publications
                WHERE publication_id = %s
                FOR UPDATE
                """,
                (identifier,),
            ).fetchone()
            if publication is None:
                return 0
            _lock_scope_writes(conn, publication["workspace_id"], publication["user_id"])
            _reject_purged(conn, publication["workspace_id"], publication["user_id"])
            cutoff = conn.execute(
                "SELECT clock_timestamp() - %s AS value",
                (timedelta(seconds=grace_seconds),),
            ).fetchone()["value"]
            if publication["state"] == "PREPARED":
                if publication["prepared_at"] > cutoff or _has_operation_owner(
                    conn,
                    str(publication["operation_id"]),
                    str(publication["workspace_id"]),
                    str(publication["user_id"]),
                ):
                    return 0
                conn.execute(
                    "UPDATE wiki_publications SET state = 'ABANDONED', abandoned_at = clock_timestamp() "
                    "WHERE publication_id = %s AND state = 'PREPARED'",
                    (identifier,),
                )
            elif (
                publication["state"] != "ABANDONED"
                or publication["abandoned_at"] is None
                or publication["abandoned_at"] > cutoff
            ):
                return 0
        # Commit the terminal fence before S3 deletion so a failed delete cannot reopen the writer.
        with connect() as conn:
            _lock_publication(conn, identifier)
            publication = conn.execute(
                "SELECT workspace_id, user_id FROM wiki_publications "
                "WHERE publication_id = %s AND state = 'ABANDONED' FOR UPDATE",
                (identifier,),
            ).fetchone()
            if publication is None:
                return 0
            _lock_scope_writes(conn, publication["workspace_id"], publication["user_id"])
            _reject_purged(conn, publication["workspace_id"], publication["user_id"])
            objects = conn.execute(
                """
                SELECT page_id, object_key
                FROM wiki_publication_objects
                WHERE publication_id = %s AND deleted_at IS NULL
                ORDER BY created_at, object_key
                """,
                (identifier,),
            ).fetchall()
            for object_row in objects:
                key = str(object_row["object_key"])
                if not _immutable_operation_key(key, workspace_id, str(object_row["page_id"])):
                    continue
                if _has_retained_reference(conn, key):
                    continue
                delete_object(key)
                conn.execute(
                    """
                    UPDATE wiki_publication_objects
                    SET deleted_at = clock_timestamp()
                    WHERE publication_id = %s AND object_key = %s AND deleted_at IS NULL
                    """,
                    (identifier, key),
                )
                deleted += 1
    return deleted


def _has_operation_owner(conn, operation_id: str, workspace_id: str, user_id: str) -> bool:
    return conn.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM ai_task_runs
            WHERE workspace_id = %s AND user_id = %s
              AND command->>'operation_id' = %s
        ) OR EXISTS (
            SELECT 1 FROM pipeline_runs
            WHERE workspace_id = %s AND status = 'running'
              AND manifest->>'operation_id' = %s
        ) AS owned
        """,
        (workspace_id, user_id, operation_id, workspace_id, operation_id),
    ).fetchone()["owned"]


def _immutable_operation_key(key: str, workspace_id: str, page_id: str) -> bool:
    parts = key.split("/")
    return (
        len(parts) == 6
        and parts[:3] == ["wiki", workspace_id, "pages"]
        and parts[3] == page_id
        and parts[4] == "ops"
        and (parts[5].endswith(".md") or parts[5].endswith(".json"))
    )


def _has_retained_reference(conn, key: str) -> bool:
    return conn.execute(
        "SELECT " + _RETAINED_REFERENCE_SQL + " AS retained",
        {"key": key},
    ).fetchone()["retained"]
