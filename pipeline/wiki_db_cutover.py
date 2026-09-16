from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    source_columns: tuple[str, ...] | None = None


TABLES = (
    Table("pipeline_runs", ("id", "document_id", "user_id", "workspace_id", "input_source", "output_dir", "mode", "status", "manifest", "error", "created_at", "updated_at", "finished_at"), ("id",)),
    Table("wiki_pages", ("id", "page_type", "title", "slug", "summary", "markdown_uri", "user_id", "workspace_id", "status", "created_at", "updated_at"), ("id",)),
    Table("document_wiki_links", ("document_id", "wiki_page_id", "relation_type", "confidence", "workspace_id", "created_at"), ("document_id", "relation_type", "wiki_page_id")),
    Table("wiki_page_links", ("from_page_id", "to_page_id", "link_type", "label", "confidence", "workspace_id", "created_at", "updated_at"), ("from_page_id", "link_type", "to_page_id")),
    Table("source_blocks", ("document_id", "block_id", "text"), ("block_id", "document_id")),
    Table("wiki_page_embeddings", ("page_id", "embedding_model", "representation_hash", "embedding_vector", "embedding_dimension", "status", "error", "created_at", "updated_at"), ("page_id", "embedding_model")),
    Table("wiki_embedding_vectors", ("id", "embedding_model", "representation_hash", "representation_text", "embedding_vector", "embedding_dimension", "status", "error", "created_at", "updated_at"), ("id",)),
    Table("wiki_embedding_units", ("id", "embedding_vector_id", "page_id", "source_document_id", "unit_type", "block_refs", "text", "weight", "created_at", "updated_at"), ("id",)),
    Table("skills", ("id", "workspace_id", "scope_type", "owner_user_id", "command", "status", "enabled_version_id", "created_at", "updated_at"), ("id",)),
    Table("skill_versions", ("id", "skill_id", "version", "name", "description", "instructions_markdown", "capabilities", "allowed_tools", "safety_result", "status", "created_by", "created_at", "published_at"), ("id",)),
    Table("agent_runs", ("id", "workspace_id", "user_id", "action", "skill_version_id", "status", "request_summary", "current_plan_id", "error_code", "tool_call_count", "document_id", "base_version", "apply_operation_id", "apply_consumed_at", "result", "created_at", "updated_at", "finished_at"), ("id",)),
    Table("skill_version_sources", ("id", "skill_version_id", "source_agent_run_id", "source_turn_id", "source_type", "created_at"), ("id",)),
    Table("agent_plans", ("id", "run_id", "version", "summary", "operation_hash", "status", "created_at", "updated_at"), ("id",)),
    Table("agent_plan_operations", ("id", "plan_id", "sequence", "tool_name", "target_type", "target_id", "base_version", "source_parent_id", "destination_parent_id", "arguments", "reason", "depends_on", "status", "error_code", "created_at", "updated_at"), ("id",)),
    Table("agent_approvals", ("id", "run_id", "plan_id", "plan_version", "operation_hash", "user_id", "decision", "created_at"), ("id",)),
    Table("agent_jobs", ("id", "run_id", "job_type", "status", "attempt_count", "available_at", "lease_owner", "lease_token", "leased_until", "heartbeat_at", "created_at", "updated_at"), ("id",)),
    Table("agent_tool_executions", ("id", "run_id", "plan_id", "operation_id", "tool_name", "idempotency_key", "attempt", "status", "response_metadata", "error_code", "finished_at"), ("id",)),
    Table(
        "agent_run_artifacts",
        ("id", "run_id", "workspace_id", "user_id", "content_hash", "purpose", "object_key", "document_id", "base_version", "target", "created_at", "expires_at"),
        ("id",),
        ("id", "run_id", "workspace_id", "user_id", "content_hash", "purpose", "document_id", "base_version", "target", "created_at", "expires_at"),
    ),
    Table("checkpoint_migrations", ("v",), ("v",)),
    Table("checkpoints", ("thread_id", "checkpoint_ns", "checkpoint_id", "parent_checkpoint_id", "type", "checkpoint", "metadata"), ("thread_id", "checkpoint_ns", "checkpoint_id")),
    Table("checkpoint_blobs", ("thread_id", "checkpoint_ns", "channel", "version", "type", "blob"), ("thread_id", "checkpoint_ns", "channel", "version")),
    Table("checkpoint_writes", ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx", "channel", "type", "blob", "task_path"), ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx")),
)

WIKI_TABLES = tuple(table.name for table in TABLES[:8])
AGENT_TABLES = (
    "skills", "skill_versions", "skill_version_sources", "agent_runs",
    "agent_plans", "agent_plan_operations", "agent_approvals", "agent_jobs",
    "agent_tool_executions", "agent_run_artifacts", "checkpoint_migrations",
    "checkpoints", "checkpoint_blobs", "checkpoint_writes",
)
SOURCE_TABLES = WIKI_TABLES + tuple(table.name for table in TABLES if table.name in AGENT_TABLES)
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "notify_pending")
SCHEMA_PATH = Path(__file__).with_name("db") / "ai_schema.sql"


def _url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Set {name} before running the Wiki DB cutover")
    return value


def _role(name: str) -> str:
    value = os.environ.get(name)
    if not value or not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
        raise RuntimeError(f"Set {name} to a PostgreSQL role identifier")
    return value


def _identifiers(values: tuple[str, ...]) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(value) for value in values)


def _stats(
    conn: psycopg.Connection,
    table: Table,
    columns: tuple[str, ...] | None = None,
) -> dict[str, str | int]:
    digest = hashlib.sha256()
    columns = columns or table.source_columns or table.columns
    query = sql.SQL(
        "SELECT jsonb_build_array({keys})::text, "
        "md5(jsonb_build_array({columns})::text) "
        "FROM {table} ORDER BY {keys}"
    ).format(
        keys=_identifiers(table.key),
        columns=_identifiers(columns),
        table=sql.Identifier(table.name),
    )
    count = 0
    with conn.cursor(name=f"cutover_hash_{table.name}") as cursor:
        cursor.execute(query)
        for key, row_hash in cursor:
            for value in (key, row_hash):
                encoded = value.encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            count += 1
    return {"count": count, "digest": digest.hexdigest()}


def _copy_table(source: psycopg.Connection, target: psycopg.Connection, table: Table) -> None:
    source_columns = table.source_columns or table.columns
    columns = _identifiers(source_columns)
    export = sql.SQL("COPY (SELECT {columns} FROM {table} ORDER BY {keys}) TO STDOUT (FORMAT BINARY)").format(
        columns=columns,
        table=sql.Identifier(table.name),
        keys=_identifiers(table.key),
    )
    load = sql.SQL("COPY {table} ({columns}) FROM STDIN (FORMAT BINARY)").format(
        table=sql.Identifier(table.name),
        columns=columns,
    )
    with source.cursor().copy(export) as output, target.cursor().copy(load) as incoming:
        for chunk in output:
            incoming.write(chunk)


def _assert_no_active_runs(source: psycopg.Connection) -> None:
    count = source.execute(
        "SELECT count(*) FROM pipeline_runs WHERE status <> ALL(%s)",
        (list(TERMINAL_RUN_STATUSES),),
    ).fetchone()[0]
    if count:
        raise RuntimeError(f"Wiki write paths are not drained: {count} pipeline run(s) are active")


def _assert_no_active_agent_runs(source: psycopg.Connection) -> None:
    count = source.execute(
        """
        SELECT count(*) FROM agent_runs
        WHERE status <> ALL(%s)
        """,
        (["completed", "partial_failed", "failed", "conflicted", "rejected", "cancelled"],),
    ).fetchone()[0]
    if count:
        raise RuntimeError(f"Agent write paths are not drained: {count} Agent run(s) are active")


def _assert_cutover_ready(source: psycopg.Connection) -> None:
    _assert_no_active_runs(source)
    _assert_no_active_agent_runs(source)
    for role in (_role("CORE_DB_RUNTIME_USER"), _role("AI_DB_RUNTIME_USER")):
        _assert_table_write(source, role, SOURCE_TABLES, False)


def _assert_no_orphans(target: psycopg.Connection) -> None:
    checks = {
        "document_wiki_links.wiki_page_id": "SELECT count(*) FROM document_wiki_links child LEFT JOIN wiki_pages parent ON parent.id=child.wiki_page_id WHERE parent.id IS NULL",
        "wiki_page_links.from_page_id": "SELECT count(*) FROM wiki_page_links child LEFT JOIN wiki_pages parent ON parent.id=child.from_page_id WHERE parent.id IS NULL",
        "wiki_page_links.to_page_id": "SELECT count(*) FROM wiki_page_links child LEFT JOIN wiki_pages parent ON parent.id=child.to_page_id WHERE parent.id IS NULL",
        "wiki_page_embeddings.page_id": "SELECT count(*) FROM wiki_page_embeddings child LEFT JOIN wiki_pages parent ON parent.id=child.page_id WHERE parent.id IS NULL",
        "wiki_embedding_units.page_id": "SELECT count(*) FROM wiki_embedding_units child LEFT JOIN wiki_pages parent ON parent.id=child.page_id WHERE parent.id IS NULL",
        "wiki_embedding_units.embedding_vector_id": "SELECT count(*) FROM wiki_embedding_units child LEFT JOIN wiki_embedding_vectors parent ON parent.id=child.embedding_vector_id WHERE parent.id IS NULL",
        "skills.enabled_version_id": "SELECT count(*) FROM skills child LEFT JOIN skill_versions parent ON parent.id=child.enabled_version_id WHERE child.enabled_version_id IS NOT NULL AND parent.id IS NULL",
        "skill_versions.skill_id": "SELECT count(*) FROM skill_versions child LEFT JOIN skills parent ON parent.id=child.skill_id WHERE parent.id IS NULL",
        "skill_version_sources.skill_version_id": "SELECT count(*) FROM skill_version_sources child LEFT JOIN skill_versions parent ON parent.id=child.skill_version_id WHERE parent.id IS NULL",
        "skill_version_sources.source_agent_run_id": "SELECT count(*) FROM skill_version_sources child LEFT JOIN agent_runs parent ON parent.id=child.source_agent_run_id WHERE child.source_agent_run_id IS NOT NULL AND parent.id IS NULL",
        "agent_plans.run_id": "SELECT count(*) FROM agent_plans child LEFT JOIN agent_runs parent ON parent.id=child.run_id WHERE parent.id IS NULL",
        "agent_runs.skill_version_id": "SELECT count(*) FROM agent_runs child LEFT JOIN skill_versions parent ON parent.id=child.skill_version_id WHERE child.skill_version_id IS NOT NULL AND parent.id IS NULL",
        "agent_runs.current_plan_id": "SELECT count(*) FROM agent_runs child LEFT JOIN agent_plans parent ON parent.id=child.current_plan_id WHERE child.current_plan_id IS NOT NULL AND parent.id IS NULL",
        "agent_plan_operations.plan_id": "SELECT count(*) FROM agent_plan_operations child LEFT JOIN agent_plans parent ON parent.id=child.plan_id WHERE parent.id IS NULL",
        "agent_approvals.run_id": "SELECT count(*) FROM agent_approvals child LEFT JOIN agent_runs parent ON parent.id=child.run_id WHERE parent.id IS NULL",
        "agent_approvals.plan_id": "SELECT count(*) FROM agent_approvals child LEFT JOIN agent_plans parent ON parent.id=child.plan_id WHERE parent.id IS NULL",
        "agent_jobs.run_id": "SELECT count(*) FROM agent_jobs child LEFT JOIN agent_runs parent ON parent.id=child.run_id WHERE parent.id IS NULL",
        "agent_tool_executions.run_id": "SELECT count(*) FROM agent_tool_executions child LEFT JOIN agent_runs parent ON parent.id=child.run_id WHERE parent.id IS NULL",
        "agent_tool_executions.plan_id": "SELECT count(*) FROM agent_tool_executions child LEFT JOIN agent_plans parent ON parent.id=child.plan_id WHERE parent.id IS NULL",
        "agent_tool_executions.operation_id": "SELECT count(*) FROM agent_tool_executions child LEFT JOIN agent_plan_operations parent ON parent.id=child.operation_id WHERE parent.id IS NULL",
        "agent_run_artifacts.run_id": "SELECT count(*) FROM agent_run_artifacts child LEFT JOIN agent_runs parent ON parent.id=child.run_id WHERE parent.id IS NULL",
    }
    failures = [name for name, query in checks.items() if target.execute(query).fetchone()[0]]
    if failures:
        raise RuntimeError(f"orphan references found: {', '.join(failures)}")


def copy_data(args: argparse.Namespace) -> None:
    if not args.writes_stopped:
        raise RuntimeError(
            "Stop Wiki and Agent workers and all mutations, then pass --writes-stopped"
        )
    with psycopg.connect(_url("CORE_DB_MIGRATION_URL")) as source, psycopg.connect(_url("AI_DB_MIGRATION_URL")) as target:
        target.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        _assert_cutover_ready(source)
        target.execute("SET CONSTRAINTS ALL DEFERRED")
        source_stats = {table.name: _stats(source, table) for table in TABLES}
        copied = []
        for table in TABLES:
            target_stat = _stats(target, table)
            if target_stat["count"] and target_stat != source_stats[table.name]:
                raise RuntimeError(
                    f"ai_db contains mismatched cutover data in {table.name}; refusing to overwrite it"
                )
            if not target_stat["count"] and source_stats[table.name]["count"]:
                _copy_table(source, target, table)
                copied.append(table.name)
        target_stats = {table.name: _stats(target, table) for table in TABLES}
        if target_stats != source_stats:
            raise RuntimeError("row count, primary key, or canonical content hash validation failed")
        _assert_no_orphans(target)
    print(json.dumps({
        "core_snapshot_id": args.core_snapshot_id,
        "ai_snapshot_id": args.ai_snapshot_id,
        "result": "copied" if copied else "validated_existing",
        "tables": source_stats,
    }, ensure_ascii=False, sort_keys=True))


def _assert_table_write(conn: psycopg.Connection, role: str, tables: tuple[str, ...], expected: bool) -> None:
    mismatches = []
    for table in tables:
        actual = conn.execute(
            "SELECT has_table_privilege(%s, %s, 'INSERT,UPDATE,DELETE')",
            (role, f"public.{table}"),
        ).fetchone()[0]
        if actual is not expected:
            mismatches.append(table)
    if mismatches:
        state = "granted" if expected else "revoked"
        raise RuntimeError(f"core write privilege was not {state}: {', '.join(mismatches)}")


def _assert_actual_write(
    conn: psycopg.Connection,
    role: str,
    tables: tuple[str, ...],
) -> None:
    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    try:
        for table in tables:
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE false").format(sql.Identifier(table))
            )
    finally:
        conn.execute("RESET ROLE")


def _assert_sequence_write(conn: psycopg.Connection, role: str) -> None:
    mismatches = []
    for (sequence,) in conn.execute(
        "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'"
    ):
        granted = conn.execute(
            "SELECT has_sequence_privilege(%s, %s, 'USAGE,SELECT,UPDATE')",
            (role, f"public.{sequence}"),
        ).fetchone()[0]
        if not granted:
            mismatches.append(sequence)
    if mismatches:
        raise RuntimeError(
            f"core sequence write privilege was not granted: {', '.join(mismatches)}"
        )


def lock_core_writes(_: argparse.Namespace) -> None:
    with psycopg.connect(_url("CORE_DB_MIGRATION_URL")) as conn:
        for role in (_role("CORE_DB_RUNTIME_USER"), _role("AI_DB_RUNTIME_USER")):
            conn.execute(sql.SQL("REVOKE INSERT, UPDATE, DELETE ON TABLE {} FROM {}").format(
                _identifiers(SOURCE_TABLES), sql.Identifier(role)
            ))
            _assert_table_write(conn, role, SOURCE_TABLES, False)


def finalize_core_permissions(args: argparse.Namespace) -> None:
    if set(args.smoke_tested) != {"ingest", "query", "lint", "restore", "agent"}:
        raise RuntimeError("Run ingest/query/lint/restore/agent smoke tests before finalizing permissions")
    ai_role = _role("AI_DB_RUNTIME_USER")
    core_role = _role("CORE_DB_RUNTIME_USER")
    with psycopg.connect(_url("CORE_DB_MIGRATION_URL")) as conn:
        database_name = psycopg.conninfo.conninfo_to_dict(_url("CORE_DB_MIGRATION_URL")).get("dbname")
        conn.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(sql.Identifier(ai_role)))
        conn.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}").format(sql.Identifier(ai_role)))
        conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {}").format(sql.Identifier(ai_role)))
        conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM {}").format(sql.Identifier(ai_role)))
        conn.execute(sql.SQL("REVOKE INSERT, UPDATE, DELETE ON TABLE {} FROM {}").format(
            _identifiers(SOURCE_TABLES), sql.Identifier(core_role)
        ))
        conn.execute(sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
            _identifiers(SOURCE_TABLES), sql.Identifier(core_role)
        ))
        _assert_table_write(conn, ai_role, ("documents", *SOURCE_TABLES), False)
        _assert_table_write(conn, core_role, SOURCE_TABLES, False)
        if database_name:
            conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                sql.Identifier(database_name), sql.Identifier(ai_role)
            ))


def rollback_core_permissions(_: argparse.Namespace) -> None:
    core_role = _role("CORE_DB_RUNTIME_USER")
    ai_role = _role("AI_DB_RUNTIME_USER")
    with psycopg.connect(_url("CORE_DB_MIGRATION_URL")) as conn:
        database_name = psycopg.conninfo.conninfo_to_dict(_url("CORE_DB_MIGRATION_URL")).get("dbname")
        if database_name:
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database_name), sql.Identifier(ai_role)
            ))
        for role in (core_role, ai_role):
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}"
            ).format(_identifiers(SOURCE_TABLES), sql.Identifier(role)))
            conn.execute(sql.SQL(
                "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}"
            ).format(sql.Identifier(role)))
            _assert_table_write(conn, role, SOURCE_TABLES, True)
            _assert_sequence_write(conn, role)
            _assert_actual_write(conn, role, SOURCE_TABLES)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="AI-owned storage maintenance cutover")
    commands = result.add_subparsers(required=True)
    copy_parser = commands.add_parser("copy")
    copy_parser.add_argument("--core-snapshot-id", required=True)
    copy_parser.add_argument("--ai-snapshot-id", required=True)
    copy_parser.add_argument("--writes-stopped", action="store_true")
    copy_parser.set_defaults(action=copy_data)
    commands.add_parser("lock-core-writes").set_defaults(action=lock_core_writes)
    finalize = commands.add_parser("finalize-core-permissions")
    finalize.add_argument("--smoke-tested", nargs="+", default=[])
    finalize.set_defaults(action=finalize_core_permissions)
    commands.add_parser("rollback-core-permissions").set_defaults(action=rollback_core_permissions)
    return result


def main() -> None:
    args = parser().parse_args()
    args.action(args)


if __name__ == "__main__":
    main()
