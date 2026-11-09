"""Cleanup crash windows and retained-reference pagination against isolated PostgreSQL/S3."""
import os
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.modules.wiki_ingestion.infrastructure import wiki_publication as publication
from app.modules.wiki_ingestion.infrastructure import wiki_publication_cleanup as cleanup
from app.modules.wiki_ingestion.infrastructure import object_storage as storage


@pytest.fixture(autouse=True)
def isolated_publication_database(monkeypatch):
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn or not os.environ.get("S3_ENDPOINT"):
        pytest.skip("isolated PostgreSQL and S3 endpoints are required")
    schema = "cleanup_review_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in dsn else "?"
    scoped = dsn + separator + "options=" + quote("-csearch_path=" + schema)
    monkeypatch.setenv("AI_DATABASE_URL", scoped)
    bucket = "cleanup-review-" + uuid4().hex
    monkeypatch.setenv("S3_BUCKET", bucket)
    try:
        with psycopg.connect(scoped) as conn:
            conn.execute((Path(__file__).resolve().parents[3] / "db/ai_schema.sql").read_text())
        yield
    finally:
        client = storage.client()
        if client.bucket_exists(bucket):
            for item in client.list_objects(bucket, recursive=True):
                client.remove_object(bucket, item.object_name)
            client.remove_bucket(bucket)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _write(operation, page="page"):
    publication.prepare_publication(operation, "ws", "user", "LINT")
    key = publication.object_key("ws", page, operation)
    publication.write_publication_text(operation, page, key, "body")
    return key


def _abandon(operation):
    with publication.connect() as conn:
        conn.execute("UPDATE wiki_publications SET state='ABANDONED', abandoned_at=now() WHERE operation_id=%s", (operation,))


def _exists(key):
    return any(item.object_name == key for item in storage.client().list_objects(storage.bucket_name(), prefix=key))


@pytest.mark.parametrize("uploaded", [False, True])
@pytest.mark.parametrize("abandoned", [False, True])
def test_cleanup_removes_upload_without_completion_marker(uploaded, abandoned):
    key = _write("unconfirmed")
    with publication.connect() as conn:
        conn.execute("UPDATE wiki_publication_objects SET uploaded_at=NULL WHERE object_key=%s", (key,))
    if not uploaded:
        storage.delete_object(key)
    if abandoned:
        _abandon("unconfirmed")
    assert cleanup.cleanup_abandoned_publications(grace_seconds=0) == 1
    assert not _exists(key)


def test_retry_does_not_recreate_object_after_cleanup_wins_fence(monkeypatch):
    key = _write("retry-race")
    original_fence = publication._user_write_fence

    @contextmanager
    def cleanup_before_upload(*args, **kwargs):
        assert cleanup.cleanup_abandoned_publications(grace_seconds=0) == 1
        with original_fence(*args, **kwargs):
            yield

    monkeypatch.setattr(publication, "_user_write_fence", cleanup_before_upload)
    with pytest.raises(publication.PublicationConflict, match="abandoned"):
        publication.write_publication_text("retry-race", "page", key, "body")
    assert not _exists(key)


def test_retained_candidate_does_not_starve_later_abandoned_objects():
    retained = _write("retained")
    _abandon("retained")
    with publication.connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,status,markdown_uri,user_id,workspace_id,created_at,updated_at) "
            "VALUES ('page','concept','Page','page','active',%s,'user','ws',now(),now())",
            (storage.storage_uri(retained),),
        )
    removable = _write("removable", "other")
    _abandon("removable")
    for _ in range(3):
        cleanup.cleanup_abandoned_publications(grace_seconds=0, limit=1)
    assert _exists(retained)
    assert not _exists(removable)
