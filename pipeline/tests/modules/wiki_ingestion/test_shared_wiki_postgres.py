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


def test_restoring_source_page_to_earlier_ingest_keeps_filename_title(scoped_dsn, monkeypatch):
    """편입 두 번 뒤 첫 편입으로 복구해도 스냅샷 본문의 생성 제목으로 바뀌지 않는다(ADR 0033)."""
    from app.modules.wiki_ingestion.application.models import SourceSnapshotRestoreCommand
    from app.modules.wiki_ingestion.infrastructure import (
        postgres_wiki_ingestion_repository as database,
    )
    from app.modules.wiki_ingestion.infrastructure.wiki_page_restore import (
        ObjectStorageWikiPageRestore,
    )

    stored: dict[str, str] = {}

    def write(key: str, text: str, *_args) -> str:
        stored[key] = text
        return key

    monkeypatch.setattr(persistence, "upload_wiki_markdown", lambda text, key: write(key, text))
    monkeypatch.setattr(persistence, "write_text_object", write)
    monkeypatch.setattr(persistence, "read_optional_text_object", lambda key: stored.get(key, ""))

    for operation_id, user_id in (("op-1", "member-a"), ("op-2", "member-b")):
        manifest = _manifest(user_id, f"{user_id} 근거")
        manifest["operation_id"] = operation_id
        manifest["source_page"] = {"markdown": f"# 생성 제목 {operation_id}\n\n## Summary\n{operation_id} 요약\n"}
        with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
            persistence.persist_wiki_outputs(conn, "doc-1", manifest)

    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        page_id = conn.execute("SELECT id FROM wiki_pages WHERE page_type = 'source'").fetchone()["id"]

    restored = ObjectStorageWikiPageRestore(
        read_text=lambda key: stored[key],
        write_text=write,
        apply_current_state_and_cleanup=lambda *_args: [],
    ).restore_source_page(
        "restore-1",
        "op-1",
        "workspace-1",
        SourceSnapshotRestoreCommand(page_id, "doc-1"),
    )
    monkeypatch.setenv("S3_BUCKET", "wiki-bucket")
    monkeypatch.setattr(database, "connect", lambda: psycopg.connect(scoped_dsn, row_factory=dict_row))
    monkeypatch.setattr(database, "read_text_object", lambda uri: stored[uri.removeprefix("s3://wiki-bucket/")])
    monkeypatch.setattr(database, "invalidate_concept_index", lambda _workspace_id: None)
    database.apply_restored_wiki_state(
        "workspace-1",
        [restored],
        {"removed_links": [], "restored_links": []},
        False,
    )

    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        row = conn.execute("SELECT title, summary, markdown_uri FROM wiki_pages WHERE id = %s", (page_id,)).fetchone()
    assert row["title"] == "공유 문서"
    assert row["summary"] == "op-1 요약"
    assert stored[row["markdown_uri"].removeprefix("s3://wiki-bucket/")].startswith("# 생성 제목 op-1")
