"""임베딩 복구 대상이 '임베딩이 덜 된 활성 페이지'를 모두 고르는지 실제 PostgreSQL로 확인한다."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.modules.wiki_embedding.infrastructure.postgres_wiki_page_embedding_repository import (
    PostgresWikiPageEmbeddingRepository,
    database,
)

DSN = os.environ.get("TEST_AGENT_DATABASE_URL") or os.environ.get("CONCEPT_LOCK_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
AI_SCHEMA_SQL = (Path(__file__).resolve().parents[3] / "db" / "ai_schema.sql").read_text(encoding="utf-8")
MODEL = "BAAI/bge-m3"


@pytest.fixture
def scoped_dsn(monkeypatch):
    name = "test_embedding_retry_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    separator = "&" if "?" in DSN else "?"
    scoped = f"{DSN}{separator}options={quote(f'-csearch_path={name}')}"
    with psycopg.connect(scoped) as conn:
        conn.execute(AI_SCHEMA_SQL)
    monkeypatch.setattr(database, "connect", lambda: psycopg.connect(scoped, row_factory=dict_row))
    try:
        yield scoped
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _page(conn, page_id: str, status: str = "active", page_vector: str | None = "completed",
          unit_vector: str | None = "completed") -> None:
    conn.execute(
        """
        INSERT INTO wiki_pages (id, page_type, title, slug, user_id, workspace_id, status, created_at, updated_at)
        VALUES (%s, 'concept', %s, %s, 'user', 'ws', %s, now(), now())
        """,
        (page_id, page_id, page_id, status),
    )
    if page_vector is not None:
        conn.execute(
            """
            INSERT INTO wiki_page_embeddings
                (page_id, embedding_model, representation_hash, embedding_vector, embedding_dimension, status)
            VALUES (%s, %s, 'hash', ARRAY[]::double precision[], 0, %s)
            """,
            (page_id, MODEL, page_vector),
        )
    if unit_vector is not None:
        conn.execute(
            "INSERT INTO wiki_embedding_vectors (id, embedding_model, representation_hash, representation_text, status)"
            " VALUES (%s, %s, %s, 'text', %s)",
            (f"vector-{page_id}", MODEL, f"hash-{page_id}", unit_vector),
        )
        conn.execute(
            "INSERT INTO wiki_embedding_units (id, embedding_vector_id, page_id, source_document_id, unit_type, text)"
            " VALUES (%s, %s, %s, 'doc', 'evidence', 'text')",
            (f"unit-{page_id}", f"vector-{page_id}", page_id),
        )


def test_retryable_pages_include_pages_without_completed_vectors(scoped_dsn) -> None:
    with psycopg.connect(scoped_dsn) as conn:
        _page(conn, "done")
        # 되돌려진 post_ingest처럼 페이지 벡터 행이 아예 없는 페이지
        _page(conn, "no-row", page_vector=None, unit_vector="pending")
        _page(conn, "page-failed", page_vector="failed")
        _page(conn, "page-pending", page_vector="pending")
        # 페이지 벡터는 끝났지만 단위 벡터가 남은 페이지
        _page(conn, "unit-pending", unit_vector="pending")
        _page(conn, "unit-failed", unit_vector="failed")
        _page(conn, "deleted", status="deleted", page_vector=None)

    page_ids = PostgresWikiPageEmbeddingRepository().list_retryable_page_ids(MODEL)

    assert sorted(page_ids) == ["no-row", "page-failed", "page-pending", "unit-failed", "unit-pending"]
