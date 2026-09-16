from unittest.mock import Mock, patch

import pytest

from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database


def _connection_with_tables(table_names: tuple[str, ...]) -> Mock:
    result = Mock()
    result.fetchall.return_value = [{"table_name": name} for name in table_names]
    connection = Mock()
    connection.execute.return_value = result
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    return connection


def test_verify_schema_accepts_all_ai_tables() -> None:
    assert "documents" not in database.AI_DB_REQUIRED_TABLES
    assert "wiki_page_contributions" not in database.AI_DB_REQUIRED_TABLES
    connection = _connection_with_tables(database.AI_DB_REQUIRED_TABLES)

    with patch.object(database, "connect_ai", return_value=connection):
        database.verify_schema()


def test_verify_schema_reports_missing_tables() -> None:
    existing_tables = tuple(
        name for name in database.AI_DB_REQUIRED_TABLES if name != "pipeline_runs"
    )
    connection = _connection_with_tables(existing_tables)

    with (
        patch.object(database, "connect_ai", return_value=connection),
        pytest.raises(RuntimeError, match="missing tables: pipeline_runs"),
    ):
        database.verify_schema()


def test_verify_agent_schema_accepts_agent_and_checkpoint_tables() -> None:
    connection = _connection_with_tables(database.AGENT_REQUIRED_TABLES)

    with patch.object(database, "connect_ai", return_value=connection):
        database.verify_agent_schema()


def test_ai_schema_contains_agent_skill_and_checkpoint_tables() -> None:
    schema = database._AI_SCHEMA_SQL_PATH.read_text(encoding="utf-8")

    for table in database.AGENT_REQUIRED_TABLES:
        assert f"{table} (" in schema


def test_cleanup_deleted_wiki_pages_removes_only_workspace_targets() -> None:
    queries = []

    def execute(query, params):
        normalized = " ".join(query.split())
        queries.append((normalized, params))
        result = Mock()
        if normalized.startswith("SELECT id, user_id FROM wiki_pages"):
            result.fetchall.return_value = [{"id": "C1", "user_id": "user-1"}]
        elif normalized.startswith("SELECT DISTINCT embedding_vector_id"):
            result.fetchall.return_value = [{"embedding_vector_id": "vector-1"}]
        else:
            result.fetchall.return_value = []
        return result

    connection = Mock()
    connection.execute.side_effect = execute
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    with patch.object(database, "connect", return_value=connection):
        database.cleanup_deleted_wiki_pages("ws-1", ["C1", "outside-page"])

    assert queries[0][1] == ("ws-1", ["C1", "outside-page"])
    assert "FOR UPDATE" in queries[0][0]
    assert any("DELETE FROM wiki_page_links" in query for query, _ in queries)
    assert any("DELETE FROM document_wiki_links" in query for query, _ in queries)
    assert any("DELETE FROM wiki_page_embeddings" in query for query, _ in queries)
    assert any("DELETE FROM wiki_embedding_units" in query for query, _ in queries)
    assert any("DELETE FROM wiki_embedding_vectors" in query for query, _ in queries)
    assert queries[-1][1] == (["C1"],)


def test_lookup_wiki_pages_casts_optional_workspace_parameter() -> None:
    connection = Mock()
    result = Mock()
    result.fetchall.return_value = [{"id": "C1"}]
    connection.execute.return_value = result
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    with patch.object(database, "connect", return_value=connection):
        assert database.lookup_wiki_pages(["C1"], "ws-1") == [{"id": "C1"}]

    query, params = connection.execute.call_args.args
    assert "(%s::text IS NULL OR workspace_id = %s)" in query
    assert params == (["C1"], "ws-1", "ws-1")


def test_delete_document_wiki_data_rejects_wrong_workspace() -> None:
    connection = Mock()
    scope_result = Mock()
    scope_result.fetchall.return_value = [{"workspace_id": "ws-other"}]
    connection.execute.return_value = scope_result
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    with (
        patch.object(database, "connect", return_value=connection),
        pytest.raises(ValueError, match="does not match workspace"),
    ):
        database.delete_document_wiki_data("ws-1", "doc-1")

    assert connection.execute.call_count == 1


def test_delete_document_wiki_data_scopes_pipeline_update() -> None:
    queries = []

    def execute(query, params):
        normalized = " ".join(query.split())
        queries.append((normalized, params))
        result = Mock()
        if normalized.startswith("SELECT DISTINCT workspace_id"):
            result.fetchall.return_value = [{"workspace_id": "ws-1"}]
        elif normalized.startswith("SELECT wiki_page_id"):
            result.fetchall.return_value = []
        else:
            result.fetchall.return_value = []
        return result

    connection = Mock()
    connection.execute.side_effect = execute
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    with patch.object(database, "connect", return_value=connection):
        database.delete_document_wiki_data("ws-1", "doc-1")

    pipeline_update = next(
        (query, params)
        for query, params in queries
        if query.startswith("UPDATE pipeline_runs")
    )
    assert "workspace_id = %s" in pipeline_update[0]
    assert pipeline_update[1] == ("doc-1", "ws-1")
