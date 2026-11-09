from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.modules.mcp_read.application.wiki_search import WikiSearchError
from app.modules.mcp_read.infrastructure import postgres_wiki_search_repository as repository_module
from app.modules.mcp_read.infrastructure.postgres_wiki_search_repository import (
    PostgresMcpWikiSearchRepository,
    SEARCH_SQL,
)
from app.modules.query.domain.entities import SemanticQueryEmbedding


DSN = os.environ.get("TEST_AGENT_DATABASE_URL") or os.environ.get("CONCEPT_LOCK_TEST_DSN")
AI_SCHEMA_SQL = (Path(__file__).resolve().parents[3] / "db" / "ai_schema.sql").read_text(encoding="utf-8")


class _CapturingConnection:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection
        self.search_params: tuple | None = None

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def execute(self, query: str, params: tuple):
        if query == SEARCH_SQL:
            self.search_params = params
        return self.connection.execute(query, params)


def _nodes(node: dict):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def _insert_page(conn: psycopg.Connection, *, page_id: str, workspace_id: str, title: str, vector: list[float]) -> None:
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
        VALUES (%s, 'test-model', %s, %s, %s, 'completed')
        """,
        (page_id, (page_id.encode().hex() * 64)[:64], vector, len(vector)),
    )


def _insert_unit(conn: psycopg.Connection, *, unit_id: str, page_id: str, text: str, vector: list[float]) -> None:
    vector_id = "vector-" + unit_id
    conn.execute(
        """
        INSERT INTO wiki_embedding_vectors
            (id, embedding_model, representation_hash, representation_text,
             embedding_vector, embedding_dimension, status)
        VALUES (%s, 'test-model', %s, %s, %s, %s, 'completed')
        """,
        (vector_id, (vector_id.encode().hex() * 64)[:64], text, vector, len(vector)),
    )
    conn.execute(
        """
        INSERT INTO wiki_embedding_units
            (id, embedding_vector_id, page_id, source_document_id, unit_type, block_refs, text)
        VALUES (%s, %s, %s, %s, 'evidence', ARRAY['B0001'], %s)
        """,
        (unit_id, vector_id, page_id, "doc-" + unit_id, text),
    )


def _call(workspace_id: str, **limits):
    return PostgresMcpWikiSearchRepository().list_candidates(
        workspace_id=workspace_id,
        query=limits.pop("query", "needle"),
        semantic_query=SemanticQueryEmbedding("test-model", limits.pop("vector", [1.0, 0.0])),
        candidate_limit=limits.pop("candidate_limit", 3),
        materialized_bytes_limit=limits.pop("materialized_bytes_limit", 10_000),
        evidence_limit=3,
        statement_timeout_ms=1000,
        scan_page_limit=limits.pop("scan_page_limit", 10),
        scan_unit_limit=limits.pop("scan_unit_limit", 20),
        vector_dimension_limit=limits.pop("vector_dimension_limit", 2),
        **limits,
    )


def _plan(scoped: str, params: tuple) -> dict:
    with psycopg.connect(scoped) as conn:
        return conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + SEARCH_SQL,
            params,
        ).fetchone()[0][0]["Plan"]


@pytest.mark.skipif(not DSN, reason="isolated PostgreSQL test URL is not configured")
def test_exact_scope_admission_precedes_text_rank_and_vector_unnest(monkeypatch) -> None:
    schema = "test_mcp_wiki_search_plan_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in DSN else "?"
    scoped = f"{DSN}{separator}options={quote(f'-csearch_path={schema}')}"
    try:
        with psycopg.connect(scoped) as conn:
            conn.execute(AI_SCHEMA_SQL)
            _insert_page(
                conn,
                page_id="page-lexical",
                workspace_id="workspace-admitted",
                title="needle lexical",
                vector=[0.0, 1.0],
            )
            _insert_unit(
                conn,
                unit_id="unit-lexical",
                page_id="page-lexical",
                text="needle evidence",
                vector=[0.0, 1.0],
            )
            _insert_page(
                conn,
                page_id="page-semantic-only",
                workspace_id="workspace-admitted",
                title="unrelated",
                vector=[1.0, 0.0],
            )
            _insert_unit(
                conn,
                unit_id="unit-semantic-only",
                page_id="page-semantic-only",
                text="unrelated evidence",
                vector=[1.0, 0.0],
            )
            for value in range(5):
                page_id = f"page-overflow-{value}"
                _insert_page(
                    conn,
                    page_id=page_id,
                    workspace_id="workspace-page-overflow",
                    title="needle common",
                    vector=[1.0, 0.0],
                )
                _insert_unit(
                    conn,
                    unit_id=f"unit-overflow-{value}",
                    page_id=page_id,
                    text="needle common evidence",
                    vector=[1.0, 0.0],
                )
            _insert_page(
                conn,
                page_id="page-unit-overflow",
                workspace_id="workspace-unit-overflow",
                title="needle common",
                vector=[1.0, 0.0],
            )
            for value in range(5):
                _insert_unit(
                    conn,
                    unit_id=f"unit-many-{value}",
                    page_id="page-unit-overflow",
                    text="needle common evidence",
                    vector=[1.0, 0.0],
                )
            _insert_page(
                conn,
                page_id="page-byte-overflow",
                workspace_id="workspace-byte-overflow",
                title="needle",
                vector=[1.0, 0.0],
            )
            _insert_unit(
                conn,
                unit_id="unit-byte-overflow",
                page_id="page-byte-overflow",
                text="x" * 1000,
                vector=[1.0, 0.0],
            )
            conn.execute("ANALYZE")

        captured = _CapturingConnection(psycopg.connect(scoped, row_factory=dict_row))
        monkeypatch.setattr(repository_module.database, "connect", lambda: captured)
        results = _call("workspace-admitted", candidate_limit=1)
        assert [result.page_id for result in results] == ["page-semantic-only"]
        assert results[0].semantic_score == pytest.approx(1.0)
        assert captured.search_params is not None
        admitted_plan = _plan(scoped, captured.search_params)
        admitted_nodes = list(_nodes(admitted_plan))
        assert any(
            node.get("Node Type") == "Function Scan"
            and node.get("Actual Rows", 0) * node.get("Actual Loops", 0) > 0
            for node in admitted_nodes
        )

        page_capture = _CapturingConnection(psycopg.connect(scoped, row_factory=dict_row))
        monkeypatch.setattr(repository_module.database, "connect", lambda: page_capture)
        with pytest.raises(WikiSearchError) as page_error:
            _call("workspace-page-overflow", scan_page_limit=3)
        assert (page_error.value.code, page_error.value.retryable) == ("DEPENDENCY_UNAVAILABLE", False)
        assert "capacity" in page_error.value.message
        assert page_capture.search_params is not None
        page_plan = _plan(scoped, page_capture.search_params)
        page_nodes = list(_nodes(page_plan))
        page_ctes = {
            node.get("Subplan Name"): node.get("Actual Rows", 0) * node.get("Actual Loops", 0)
            for node in page_nodes
            if node.get("Subplan Name")
        }
        assert page_ctes["CTE scope_pages_probe"] == 4
        assert page_ctes.get("CTE scope_units_probe", 0) == 0
        assert page_ctes.get("CTE lexical_scores", 0) == 0
        assert page_ctes.get("CTE semantic_page_scores", 0) == 0
        assert all(
            node.get("Actual Rows", 0) * node.get("Actual Loops", 0) == 0
            for node in page_nodes
            if node.get("Node Type") == "Function Scan"
        )

        unit_capture = _CapturingConnection(psycopg.connect(scoped, row_factory=dict_row))
        monkeypatch.setattr(repository_module.database, "connect", lambda: unit_capture)
        with pytest.raises(WikiSearchError) as unit_error:
            _call("workspace-unit-overflow", scan_unit_limit=3)
        assert unit_error.value.retryable is False
        assert unit_capture.search_params is not None
        unit_plan = _plan(scoped, unit_capture.search_params)
        unit_ctes = {
            node.get("Subplan Name"): node.get("Actual Rows", 0) * node.get("Actual Loops", 0)
            for node in _nodes(unit_plan)
            if node.get("Subplan Name")
        }
        assert unit_ctes["CTE scope_units_probe"] == 4
        assert unit_ctes.get("CTE lexical_scores", 0) == 0
        assert unit_ctes.get("CTE semantic_page_scores", 0) == 0

        byte_capture = _CapturingConnection(psycopg.connect(scoped, row_factory=dict_row))
        monkeypatch.setattr(repository_module.database, "connect", lambda: byte_capture)
        with pytest.raises(WikiSearchError) as byte_error:
            _call("workspace-byte-overflow", materialized_bytes_limit=100)
        assert byte_error.value.retryable is False

        page_probe = SEARCH_SQL.split("scope_pages_probe AS MATERIALIZED", 1)[1].split(
            "page_admission AS MATERIALIZED", 1
        )[0]
        unit_probe = SEARCH_SQL.split("scope_units_probe AS MATERIALIZED", 1)[1].split(
            "unit_admission AS MATERIALIZED", 1
        )[0]
        assert "page.markdown_uri" not in page_probe
        assert "unit.text," not in unit_probe
        assert "LIMIT %s" in page_probe and "LIMIT %s" in unit_probe
        assert "idx_wiki_pages_active_search" not in AI_SCHEMA_SQL
        assert "idx_wiki_embedding_units_search" not in AI_SCHEMA_SQL
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
