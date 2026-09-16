"""AI 소유 행을 변경 기록의 역순으로 복구한다. 뒤에 들어온 변경은 덮어쓰지 않는다."""

import hashlib
import json
from contextlib import contextmanager
from typing import Any

import psycopg
from minio.error import S3Error
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.pipeline_control import PipelineRunCancelledError, task_run_id
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import ai_database_url


TABLES = frozenset({"pipeline_runs", "wiki_pages", "document_wiki_links", "wiki_page_links", "source_blocks",
                    "wiki_page_embeddings", "wiki_embedding_vectors", "wiki_embedding_units", "wiki_schemas",
                    "document_derived_state", "skills", "skill_versions", "skill_version_sources"})
class TaskAlreadyExecutingError(RuntimeError):
    pass


STOPPING = frozenset({"cancel_requested", "rolling_back", "rollback_failed", "cancelled"})


def connect():
    # 제어 기록의 조회·복구는 새 변경 기록을 만들지 않는다.
    return psycopg.connect(ai_database_url(), row_factory=dict_row)


def register(command: dict[str, Any]) -> dict[str, Any]:
    if command.get("kind") not in {"query", "document", "chat_wiki", "lint", "restore_ingest", "restore_lint", "post_ingest", "ingest", "skill_author", "skill_publish", "skill_update"}:
        raise ValueError("Unsupported task kind.")
    digest = hashlib.sha256(json.dumps(command, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with connect() as conn:
        parent_id = command.get("ingest_run_id")
        if parent_id:
            parent = conn.execute("SELECT status, workspace_id, user_id FROM ai_task_runs WHERE id = %s FOR SHARE", (parent_id,)).fetchone()
            if (parent is None or parent["status"] in STOPPING
                    or parent["workspace_id"] != command["workspace_id"] or parent["user_id"] != command["user_id"]):
                raise PipelineRunCancelledError("Parent task is unavailable.")
        conn.execute(
            "INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, parent_run_id, command_hash, command) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT(id) DO NOTHING",
            (command["run_id"], command["workspace_id"], command["user_id"], command["kind"], parent_id, digest, Jsonb(command)),
        )
        row = conn.execute("SELECT * FROM ai_task_runs WHERE id = %s", (command["run_id"],)).fetchone()
    if row["command_hash"] != digest:
        raise ValueError("Task command identity mismatch.")
    return row


def active(run_id: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT status FROM ai_task_runs WHERE id = %s", (run_id,)).fetchone()
    return row is not None and row["status"] == "running"


@contextmanager
def execution_lock(run_id: str):
    with connect() as conn:
        acquired = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                                ("ai-task:" + run_id,)).fetchone()["acquired"]
        if not acquired:
            raise TaskAlreadyExecutingError("Task is already executing.")
        try:
            yield
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", ("ai-task:" + run_id,))


def complete(run_id: str, result: dict[str, Any]) -> None:
    with connect() as conn:
        row = conn.execute("UPDATE ai_task_runs SET status = 'completed', result = %s, updated_at = now() "
                           "WHERE id = %s AND status = 'running' RETURNING id", (Jsonb(result), run_id)).fetchone()
    if row is None:
        raise PipelineRunCancelledError("Task no longer accepts a result.")


def request_cancel(run_id: str, workspace_id: str, user_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM ai_task_runs WHERE id = %s AND workspace_id = %s AND user_id = %s FOR UPDATE",
                           (run_id, workspace_id, user_id)).fetchone()
        if row is None:
            raise KeyError("Task not found.")
        conn.execute("WITH RECURSIVE tree AS (SELECT id FROM ai_task_runs WHERE id = %s UNION ALL "
                     "SELECT child.id FROM ai_task_runs child JOIN tree ON child.parent_run_id = tree.id) "
                     "UPDATE ai_task_runs SET status = 'cancel_requested', error_code = NULL, updated_at = now() "
                     "WHERE id IN (SELECT id FROM tree) AND status NOT IN ('rolling_back', 'cancelled')", (run_id,))
    return status(run_id, workspace_id, user_id)


def status(run_id: str, workspace_id: str, user_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT id, status, error_code FROM ai_task_runs "
                           "WHERE id = %s AND workspace_id = %s AND user_id = %s",
                           (run_id, workspace_id, user_id)).fetchone()
    if row is None:
        raise KeyError("Task not found.")
    return row


def rollback(run_id: str) -> None:
    with connect() as conn:
        row = conn.execute("UPDATE ai_task_runs SET status = 'rolling_back', updated_at = now() "
                           "WHERE id = %s AND status IN ('cancel_requested', 'rolling_back', 'rollback_failed') RETURNING id",
                           (run_id,)).fetchone()
        if row is None:
            return
        child = conn.execute("SELECT 1 FROM ai_task_runs WHERE parent_run_id = %s AND status <> 'cancelled' LIMIT 1",
                             (run_id,)).fetchone()
        if child:
            conn.execute("UPDATE ai_task_runs SET status = 'cancel_requested' WHERE id = %s", (run_id,))
            return
        changes = conn.execute("SELECT * FROM ai_task_changes WHERE run_id = %s AND NOT undone ORDER BY id DESC",
                               (run_id,)).fetchall()
    try:
        for change in changes:
            if change["table_name"] == "__object__":
                from app.modules.task_cancellation.infrastructure.object_change_journal import undo_object
                undo_object(change)
            else:
                undo_row(change)
    except (ValueError, psycopg.Error, S3Error, OSError) as exc:
        with connect() as conn:
            conn.execute("UPDATE ai_task_runs SET status = 'rollback_failed', error_code = %s, updated_at = now() WHERE id = %s",
                         ("rollback_conflict" if isinstance(exc, ValueError) else "rollback_database_error", run_id))
        return
    with connect() as conn:
        conn.execute("UPDATE ai_task_runs SET status = 'cancelled', error_code = NULL, updated_at = now() WHERE id = %s",
                     (run_id,))


def undo_row(change: dict[str, Any]) -> None:
    name = change["table_name"]
    if name not in TABLES:
        raise ValueError("Unsupported rollback table.")
    table = sql.Identifier(name)
    keys = change["row_key"]
    predicate = sql.SQL(" AND ").join(sql.SQL("t.{} = k.{}").format(sql.Identifier(k), sql.Identifier(k)) for k in keys)
    with connect() as conn:
        row = conn.execute(sql.SQL("SELECT to_jsonb(t) AS snapshot FROM {} t, jsonb_populate_record(NULL::{}, %s) k "
                                   "WHERE {} FOR UPDATE OF t").format(table, table, predicate), (Jsonb(keys),)).fetchone()
        current = row["snapshot"] if row else None
        if current != change["after_value"]:
            raise ValueError("Rollback would overwrite a later change.")
        before = change["before_value"]
        if before is None:
            _require_no_references(conn, name, current)
            conn.execute(sql.SQL("DELETE FROM {} t USING jsonb_populate_record(NULL::{}, %s) k WHERE {}")
                         .format(table, table, predicate), (Jsonb(keys),))
        elif current is None:
            conn.execute(sql.SQL("INSERT INTO {} SELECT * FROM jsonb_populate_record(NULL::{}, %s)").format(table, table),
                         (Jsonb(before),))
        else:
            columns = sql.SQL(", ").join(map(sql.Identifier, before))
            conn.execute(sql.SQL("UPDATE {} t SET ({}) = (SELECT {} FROM jsonb_populate_record(NULL::{}, %s)) "
                                 "FROM jsonb_populate_record(NULL::{}, %s) k WHERE {}")
                         .format(table, columns, columns, table, table, predicate), (Jsonb(before), Jsonb(keys)))
        conn.execute("UPDATE ai_task_changes SET undone = true WHERE id = %s", (change["id"],))


def _require_no_references(conn, table_name: str, value: dict[str, Any]) -> None:
    references = conn.execute("""
        SELECT c.conrelid::regclass::text AS child_table,
               array_agg(child.attname ORDER BY pair.ordinality) AS child_keys,
               array_agg(parent.attname ORDER BY pair.ordinality) AS parent_keys
        FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY pair(child_no, parent_no, ordinality)
        JOIN pg_attribute child ON child.attrelid = c.conrelid AND child.attnum = pair.child_no
        JOIN pg_attribute parent ON parent.attrelid = c.confrelid AND parent.attnum = pair.parent_no
        WHERE c.contype = 'f' AND c.confrelid = %s::regclass GROUP BY c.oid, c.conrelid
    """, (table_name,)).fetchall()
    for reference in references:
        # 다른 작업이 추가한 자식까지 ON DELETE CASCADE로 지우지 않는다.
        child = sql.Identifier(*reference["child_table"].split("."))
        matches = sql.SQL(" AND ").join(sql.SQL("to_jsonb(t)->{} = %s::jsonb")
                                        .format(sql.Literal(key)) for key in reference["child_keys"])
        if conn.execute(sql.SQL("SELECT 1 FROM {} t WHERE {} LIMIT 1").format(child, matches),
                        tuple(Jsonb(value[key]) for key in reference["parent_keys"])).fetchone():
            raise ValueError("Rollback target has surviving references.")


def execute(command: dict[str, Any], handle) -> dict[str, Any]:
    from app.core.pipeline_control import task_cancellation_scope, ensure_task_active
    run_id = str(command["run_id"])
    with execution_lock(run_id):
        row = register(command)
        if row["status"] == "completed":
            return row["result"] or {}
        if row["status"] == "failed":
            raise ValueError(row["error_code"] or "Task failed.")
        if row["status"] in STOPPING:
            rollback(run_id)
            raise PipelineRunCancelledError("Task cancellation requested.")
        with connect() as conn:
            interrupted = conn.execute("SELECT 1 FROM ai_task_changes WHERE run_id = %s AND NOT undone LIMIT 1",
                                       (run_id,)).fetchone()
        if interrupted:
            request_cancel(run_id, row["workspace_id"], row["user_id"])
            rollback(run_id)
            raise PipelineRunCancelledError("Interrupted task changes were sent for rollback.")
        token = task_run_id.set(run_id)
        try:
            with task_cancellation_scope(lambda: active(run_id)):
                result = handle()
                ensure_task_active()
                complete(run_id, result)
                return result
        except BaseException:
            cancelled = not active(run_id)
            task_run_id.reset(token)
            token = None
            request_cancel(run_id, row["workspace_id"], row["user_id"])
            rollback(run_id)
            if not cancelled:
                with connect() as conn:
                    conn.execute("UPDATE ai_task_runs SET status = 'failed', error_code = 'task_failed' "
                                 "WHERE id = %s AND status = 'cancelled'", (run_id,))
            raise
        finally:
            if token is not None:
                task_run_id.reset(token)


def rollback_pending() -> None:
    with connect() as conn:
        conn.execute("UPDATE ai_task_runs parent SET status = 'rollback_failed', error_code = 'rollback_child_failed' "
                     "WHERE parent.status IN ('cancel_requested', 'rolling_back') AND EXISTS "
                     "(SELECT 1 FROM ai_task_runs child WHERE child.parent_run_id = parent.id AND child.status = 'rollback_failed')")
        rows = conn.execute("SELECT id FROM ai_task_runs task WHERE status IN ('cancel_requested', 'rolling_back') "
                            "AND NOT EXISTS (SELECT 1 FROM ai_task_runs child WHERE child.parent_run_id = task.id "
                            "AND child.status <> 'cancelled') ORDER BY created_at DESC LIMIT 20").fetchall()
    for row in rows:
        try:
            with execution_lock(row["id"]):
                rollback(row["id"])
        except TaskAlreadyExecutingError:
            continue


def cancel_document_tasks(document_id: str, workspace_id: str, user_id: str) -> bool:
    with connect() as conn:
        rows = conn.execute("SELECT id FROM ai_task_runs WHERE workspace_id = %s AND user_id = %s "
                            "AND command->>'document_id' = %s", (workspace_id, user_id, document_id)).fetchall()
    for row in rows:
        request_cancel(row["id"], workspace_id, user_id)
    rollback_pending()
    # 자식 복구가 끝난 뒤 부모가 같은 호출에서 진행할 수 있다.
    rollback_pending()
    with connect() as conn:
        pending = conn.execute("SELECT 1 FROM ai_task_runs WHERE workspace_id = %s AND command->>'document_id' = %s "
                               "AND status <> 'cancelled' LIMIT 1", (workspace_id, document_id)).fetchone()
        links = conn.execute("SELECT 1 FROM document_wiki_links WHERE workspace_id = %s AND document_id = %s LIMIT 1",
                             (workspace_id, document_id)).fetchone()
        blocks = conn.execute("SELECT 1 FROM source_blocks WHERE document_id = %s LIMIT 1", (document_id,)).fetchone()
    return not (pending or links or blocks)
