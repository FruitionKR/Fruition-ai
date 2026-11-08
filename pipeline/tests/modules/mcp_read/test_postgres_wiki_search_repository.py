from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.modules.mcp_read.application.wiki_search import WikiSearchError
from app.modules.mcp_read.infrastructure.postgres_wiki_search_repository import PostgresMcpWikiSearchRepository
from app.modules.query.domain.entities import SemanticQueryEmbedding
from app.modules.mcp_read.infrastructure import postgres_wiki_search_repository as repository_module


class Rows:
    def __init__(self, rows=None) -> None:
        self._rows = rows or []

    def fetchall(self):
        return self._rows


class Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql: str, params: tuple):
        assert sql.count("%s") == len(params)
        self.calls.append((sql, params))
        return Rows()


def test_sql_bounds_workspace_rows_bytes_evidence_and_deadline_before_materialization(monkeypatch) -> None:
    connection = Connection()
    monkeypatch.setattr(
        "app.modules.mcp_read.infrastructure.postgres_wiki_search_repository.database.connect",
        lambda: connection,
    )

    PostgresMcpWikiSearchRepository().list_candidates(
        workspace_id="workspace-target",
        query="bounded query",
        semantic_query=SemanticQueryEmbedding("test-model", [1.0, 0.0]),
        candidate_limit=7,
        materialized_bytes_limit=2048,
        evidence_limit=9,
        statement_timeout_ms=250,
        scan_page_limit=20,
        scan_unit_limit=40,
        vector_dimension_limit=2,
    )

    timeout_sql, timeout_params = connection.calls[0]
    search_sql, search_params = connection.calls[1]
    assert "set_config('statement_timeout'" in timeout_sql
    assert timeout_params == ("250ms",)
    assert "page.workspace_id = %s" in search_sql
    assert "page.status = 'active'" in search_sql
    assert "LIMIT %s" in search_sql
    assert "scope_pages_probe AS MATERIALIZED" in search_sql
    assert "scope_units_probe AS MATERIALIZED" in search_sql
    assert "admission AS MATERIALIZED" in search_sql
    assert search_sql.index("admission AS MATERIALIZED") < search_sql.index("lexical_scores AS MATERIALIZED")
    assert search_sql.index("admission AS MATERIALIZED") < search_sql.index("semantic_page_scores AS MATERIALIZED")
    assert "FROM admitted_pages page" in search_sql
    assert "(best.block_refs)[1:%s]" in search_sql
    assert "string_agg" not in search_sql
    assert search_params.count("workspace-target") == 1
    assert 21 in search_params
    assert 41 in search_params
    assert 2048 in search_params
    assert 10 in search_params


def test_repository_materializes_only_bounded_safe_projection(monkeypatch) -> None:
    class RowConnection(Connection):
        def execute(self, sql: str, params: tuple):
            self.calls.append((sql, params))
            if "set_config" in sql:
                return Rows()
            return Rows([{
                "capacity_exceeded": False,
                "id": "historical-page-id",
                "page_type": "source",
                "title": "Title",
                "summary": "Summary",
                "version": 123,
                "content_hash": "sha256:" + "a" * 64,
                "embedding_text": "bounded snippet source",
                "source_document_id": "doc-a",
                "block_refs": ["B0001"],
                "evidence_count": 1,
                "materialized_bytes": 42,
                "similarity": 0.75,
                "raw_internal_payload": "must not escape",
            }])

    connection = RowConnection()
    monkeypatch.setattr(
        "app.modules.mcp_read.infrastructure.postgres_wiki_search_repository.database.connect",
        lambda: connection,
    )
    [result] = PostgresMcpWikiSearchRepository().list_candidates(
        workspace_id="workspace-target",
        query="bounded query",
        semantic_query=SemanticQueryEmbedding("test-model", [1.0, 0.0]),
        candidate_limit=7,
        materialized_bytes_limit=2048,
        evidence_limit=9,
        statement_timeout_ms=250,
        scan_page_limit=20,
        scan_unit_limit=40,
        vector_dimension_limit=2,
    )

    assert result.page_id == "historical-page-id"
    assert result.block_refs == ("B0001",)
    assert result.semantic_score == 0.75
    assert not hasattr(result, "raw_internal_payload")


DSN = os.environ.get("TEST_AGENT_DATABASE_URL") or os.environ.get("CONCEPT_LOCK_TEST_DSN")
AI_SCHEMA_SQL = (Path(__file__).resolve().parents[3] / "db" / "ai_schema.sql").read_text(encoding="utf-8")


@pytest.mark.skipif(not DSN, reason="isolated PostgreSQL test URL is not configured")
def test_postgres_filters_cross_workspace_and_enforces_materialized_byte_bound(monkeypatch) -> None:
    schema = "test_mcp_wiki_search_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in DSN else "?"
    scoped = f"{DSN}{separator}options={quote(f'-csearch_path={schema}')}"
    try:
        with psycopg.connect(scoped) as conn:
            conn.execute(AI_SCHEMA_SQL)
            for page_id, workspace_id, title in (
                ("page-target", "workspace-target", "bounded query"),
                ("page-other", "workspace-other", "bounded query"),
                ("page-too-large", "workspace-too-large", "bounded query large"),
            ):
                conn.execute(
                    """
                    INSERT INTO wiki_pages
                        (id, page_type, title, slug, summary, user_id, workspace_id, status, created_at, updated_at)
                    VALUES (%s, 'concept', %s, %s, '', 'user', %s, 'active', now(), now())
                    """,
                    (page_id, title, page_id, workspace_id),
                )
                conn.execute(
                    """
                    INSERT INTO wiki_page_embeddings
                        (page_id, embedding_model, representation_hash, embedding_vector, embedding_dimension, status)
                    VALUES (%s, 'test-model', %s, ARRAY[1.0, 0.0], 2, 'completed')
                    """,
                    (page_id, {"page-target": "a", "page-other": "b", "page-too-large": "c"}[page_id] * 64),
                )
                text = "x" * 5000 if page_id == "page-too-large" else "bounded query evidence"
                conn.execute(
                    """
                    INSERT INTO wiki_embedding_vectors
                        (id, embedding_model, representation_hash, representation_text,
                         embedding_vector, embedding_dimension, status)
                    VALUES (%s, 'test-model', %s, %s, ARRAY[1.0, 0.0], 2, 'completed')
                    """,
                    (
                        f"vector-{page_id}",
                        ("c" if page_id == "page-target" else "d" if page_id == "page-other" else "e") * 64,
                        text,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO wiki_embedding_units
                        (id, embedding_vector_id, page_id, source_document_id, unit_type, block_refs, text)
                    VALUES (%s, %s, %s, %s, 'evidence', ARRAY['B0001'], %s)
                    """,
                    (f"unit-{page_id}", f"vector-{page_id}", page_id, f"doc-{page_id}", text),
                )
        monkeypatch.setattr(
            repository_module.database,
            "connect",
            lambda: psycopg.connect(scoped, row_factory=dict_row),
        )

        results = PostgresMcpWikiSearchRepository().list_candidates(
            workspace_id="workspace-target",
            query="bounded query",
            semantic_query=SemanticQueryEmbedding("test-model", [1.0, 0.0]),
            candidate_limit=10,
            materialized_bytes_limit=1000,
            evidence_limit=10,
            statement_timeout_ms=1000,
            scan_page_limit=10,
            scan_unit_limit=10,
            vector_dimension_limit=2,
        )

        assert [result.page_id for result in results] == ["page-target"]
        assert sum(result.materialized_bytes for result in results) <= 1000

        with pytest.raises(WikiSearchError) as oversized:
            PostgresMcpWikiSearchRepository().list_candidates(
                workspace_id="workspace-too-large",
                query="bounded query",
                semantic_query=SemanticQueryEmbedding("test-model", [1.0, 0.0]),
                candidate_limit=10,
                materialized_bytes_limit=1000,
                evidence_limit=10,
                statement_timeout_ms=1000,
                scan_page_limit=10,
                scan_unit_limit=10,
                vector_dimension_limit=2,
            )
        assert (oversized.value.code, oversized.value.retryable) == ("DEPENDENCY_UNAVAILABLE", False)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
