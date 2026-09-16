from argparse import Namespace
from unittest.mock import Mock, call, patch

import pytest

import wiki_db_cutover as cutover


def test_copy_requires_all_wiki_writes_to_be_stopped() -> None:
    with pytest.raises(RuntimeError, match="Stop Wiki and Agent workers"):
        cutover.copy_data(Namespace(writes_stopped=False))


def test_finalize_requires_all_smoke_tests() -> None:
    with pytest.raises(RuntimeError, match="ingest/query/lint/restore/agent"):
        cutover.finalize_core_permissions(Namespace(smoke_tested=["ingest", "query"]))


def test_cutover_ready_requires_drained_runs_and_revoked_runtime_writes(monkeypatch) -> None:
    monkeypatch.setenv("CORE_DB_RUNTIME_USER", "core_runtime")
    monkeypatch.setenv("AI_DB_RUNTIME_USER", "ai_runtime")
    source = Mock()

    with (
        patch.object(cutover, "_assert_no_active_runs") as no_active_runs,
        patch.object(cutover, "_assert_no_active_agent_runs") as no_active_agent_runs,
        patch.object(cutover, "_assert_table_write") as table_write,
    ):
        cutover._assert_cutover_ready(source)

    no_active_runs.assert_called_once_with(source)
    no_active_agent_runs.assert_called_once_with(source)
    assert table_write.call_args_list == [
        call(source, "core_runtime", cutover.SOURCE_TABLES, False),
        call(source, "ai_runtime", cutover.SOURCE_TABLES, False),
    ]


def test_permission_check_rejects_remaining_write_access() -> None:
    result = Mock()
    result.fetchone.return_value = (True,)
    connection = Mock()
    connection.execute.return_value = result

    with pytest.raises(RuntimeError, match="wiki_pages"):
        cutover._assert_table_write(connection, "ai_runtime", ("wiki_pages",), False)


def test_cutover_copies_every_ai_owned_current_state_table() -> None:
    assert cutover.WIKI_TABLES == (
        "pipeline_runs",
        "wiki_pages",
        "document_wiki_links",
        "wiki_page_links",
        "source_blocks",
        "wiki_page_embeddings",
        "wiki_embedding_vectors",
        "wiki_embedding_units",
    )


def test_cutover_includes_agent_skill_and_checkpoint_tables() -> None:
    assert {table.name for table in cutover.TABLES}.issuperset(cutover.AGENT_TABLES)
    assert {"agent_runs", "skills", "checkpoints", "checkpoint_writes"}.issubset(
        cutover.AGENT_TABLES
    )


def test_agent_artifact_cutover_snapshot_keeps_v25_source_without_object_key() -> None:
    artifact_table = next(table for table in cutover.TABLES if table.name == "agent_run_artifacts")

    assert "object_key" in artifact_table.columns
    assert artifact_table.source_columns == (
        "id", "run_id", "workspace_id", "user_id", "content_hash", "purpose",
        "document_id", "base_version", "target", "created_at", "expires_at",
    )
    assert "object_key" not in artifact_table.source_columns


def test_cutover_rejects_active_agent_runs() -> None:
    result = Mock()
    result.fetchone.return_value = (1,)
    source = Mock()
    source.execute.return_value = result

    with pytest.raises(RuntimeError, match="Agent write paths are not drained"):
        cutover._assert_no_active_agent_runs(source)


def test_ai_schema_keeps_pipeline_workspace_status_index() -> None:
    schema = cutover.SCHEMA_PATH.read_text(encoding="utf-8")
    assert "idx_pipeline_runs_workspace_status" in schema
    assert "(workspace_id, status, created_at DESC)" in schema


def test_copy_rejects_nonempty_mismatched_target_without_copying(monkeypatch) -> None:
    monkeypatch.setenv("CORE_DB_MIGRATION_URL", "postgresql://source")
    monkeypatch.setenv("AI_DB_MIGRATION_URL", "postgresql://target")
    source = Mock()
    target = Mock()
    source.__enter__ = Mock(return_value=source)
    source.__exit__ = Mock(return_value=False)
    target.__enter__ = Mock(return_value=target)
    target.__exit__ = Mock(return_value=False)

    def stats(connection, table):
        return {"count": 1, "digest": "source" if connection is source else "target"}

    with (
        patch.object(cutover.psycopg, "connect", side_effect=[source, target]),
        patch.object(cutover, "_assert_cutover_ready"),
        patch.object(cutover, "_stats", side_effect=stats),
        patch.object(cutover, "_copy_table") as copy_table,
        pytest.raises(RuntimeError, match="mismatched cutover data"),
    ):
        cutover.copy_data(Namespace(
            writes_stopped=True,
            core_snapshot_id="core-snapshot",
            ai_snapshot_id="ai-snapshot",
        ))

    copy_table.assert_not_called()


def test_finalize_denies_core_dml_for_both_runtime_roles(monkeypatch) -> None:
    monkeypatch.setenv("CORE_DB_MIGRATION_URL", "postgresql://migration")
    monkeypatch.setenv("CORE_DB_RUNTIME_USER", "core_runtime")
    monkeypatch.setenv("AI_DB_RUNTIME_USER", "ai_runtime")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    with (
        patch.object(cutover.psycopg, "connect", return_value=connection),
        patch.object(cutover, "_assert_table_write") as table_write,
    ):
        cutover.finalize_core_permissions(Namespace(
            smoke_tested=["ingest", "query", "lint", "restore", "agent"]
        ))

    assert table_write.call_args_list == [
        call(connection, "ai_runtime", ("documents", *cutover.SOURCE_TABLES), False),
        call(connection, "core_runtime", cutover.SOURCE_TABLES, False),
    ]


def test_rollback_restores_and_probes_both_runtime_roles(monkeypatch) -> None:
    monkeypatch.setenv("CORE_DB_MIGRATION_URL", "postgresql://migration")
    monkeypatch.setenv("CORE_DB_RUNTIME_USER", "core_runtime")
    monkeypatch.setenv("AI_DB_RUNTIME_USER", "ai_runtime")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    with (
        patch.object(cutover.psycopg, "connect", return_value=connection),
        patch.object(cutover, "_assert_table_write") as table_write,
        patch.object(cutover, "_assert_sequence_write") as sequence_write,
        patch.object(cutover, "_assert_actual_write") as actual_write,
    ):
        cutover.rollback_core_permissions(Namespace())

    assert table_write.call_args_list == [
        call(connection, "core_runtime", cutover.SOURCE_TABLES, True),
        call(connection, "ai_runtime", cutover.SOURCE_TABLES, True),
    ]
    assert actual_write.call_args_list == [
        call(connection, "core_runtime", cutover.SOURCE_TABLES),
        call(connection, "ai_runtime", cutover.SOURCE_TABLES),
    ]
    assert sequence_write.call_args_list == [
        call(connection, "core_runtime"),
        call(connection, "ai_runtime"),
    ]
