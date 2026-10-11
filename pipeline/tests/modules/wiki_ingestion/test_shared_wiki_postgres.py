"""위키 공유 저장(ADR 0033)을 실제 PostgreSQL 고유 키로 확인한다. CI는 격리 DB를 준다."""
import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_output_persistence as persistence,
)

DSN = os.environ.get("TEST_AGENT_DATABASE_URL") or os.environ.get("CONCEPT_LOCK_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")

AI_SCHEMA_SQL = (Path(__file__).resolve().parents[3] / "db" / "ai_schema.sql").read_text(encoding="utf-8")


@pytest.fixture
def scoped_dsn():
    name = "test_shared_wiki_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    separator = "&" if "?" in DSN else "?"
    scoped = f"{DSN}{separator}options={quote(f'-csearch_path={name}')}"
    with psycopg.connect(scoped) as conn:
        conn.execute(AI_SCHEMA_SQL)
    try:
        yield scoped
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _manifest(user_id: str, claim: str) -> dict:
    return {
        "normalized": {
            "document": {"title": "문서"},
            "concept_ledger": [{"slug": "shared", "title": "Shared", "definition": "공유 개념"}],
        },
        "display_name": "공유 문서",
        "source_page": {"markdown": "# 공유 문서\n"},
        "source_blocks": [],
        "concept_pages": [{"slug": "shared", "markdown": f"# Shared\n\n## Evidence\n- {claim}\n"}],
        "links": [],
        "user_id": user_id,
        "workspace_id": "workspace-1",
    }


def test_two_members_ingesting_same_document_and_concept_share_one_page(scoped_dsn, monkeypatch):
    stored: dict[str, str] = {}

    def write(markdown: str, key: str) -> str:
        stored[key] = markdown
        return key

    monkeypatch.setattr(persistence, "upload_wiki_markdown", write)
    monkeypatch.setattr(persistence, "read_optional_text_object", lambda key: stored.get(key, ""))

    for user_id in ("member-a", "member-b"):
        with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
            persistence.persist_wiki_outputs(conn, "doc-1", _manifest(user_id, f"{user_id} 근거"))

    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT page_type, slug, title, user_id FROM wiki_pages ORDER BY page_type"
        ).fetchall()
    assert rows == [
        {"page_type": "concept", "slug": "shared", "title": "Shared", "user_id": "member-b"},
        {"page_type": "source", "slug": "doc-1", "title": "공유 문서", "user_id": "member-b"},
    ]


def _legacy_wiki_pages(conn: psycopg.Connection) -> None:
    conn.execute("ALTER TABLE wiki_pages DROP CONSTRAINT uq_wiki_pages_workspace_page_type_slug")
    conn.execute(
        "ALTER TABLE wiki_pages ADD CONSTRAINT uq_wiki_pages_workspace_type_slug "
        "UNIQUE (user_id, workspace_id, page_type, slug)"
    )
    for page_id, user_id in (("page-a", "member-a"), ("page-b", "member-b")):
        conn.execute(
            "INSERT INTO wiki_pages (id, page_type, title, slug, user_id, workspace_id, status, created_at, updated_at) "
            "VALUES (%s, 'concept', 'Shared', 'shared', %s, 'workspace-1', 'active', now(), now())",
            (page_id, user_id),
        )


def _constraints(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'wiki_pages'::regclass AND contype = 'u'"
    ).fetchall()
    return {row[0] for row in rows}


def test_schema_refuses_startup_while_per_user_duplicates_remain(scoped_dsn):
    with psycopg.connect(scoped_dsn) as conn:
        _legacy_wiki_pages(conn)

    with psycopg.connect(scoped_dsn) as conn, pytest.raises(psycopg.errors.RaiseException) as raised:
        conn.execute(AI_SCHEMA_SQL)
    assert "위키 데이터를 초기화한 뒤 배포하세요" in str(raised.value)

    with psycopg.connect(scoped_dsn) as conn:
        assert _constraints(conn) == {"uq_wiki_pages_workspace_type_slug"}


def test_schema_replaces_per_user_key_once_wiki_is_reset(scoped_dsn):
    with psycopg.connect(scoped_dsn) as conn:
        _legacy_wiki_pages(conn)
        conn.execute("DELETE FROM wiki_pages")

    for _ in range(2):
        with psycopg.connect(scoped_dsn) as conn:
            conn.execute(AI_SCHEMA_SQL)

    with psycopg.connect(scoped_dsn) as conn:
        assert _constraints(conn) == {"uq_wiki_pages_workspace_page_type_slug"}
