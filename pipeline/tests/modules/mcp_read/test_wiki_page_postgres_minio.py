from __future__ import annotations

import hashlib
import os
import socket
import socketserver
import threading
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlparse
from uuid import uuid4

import psycopg
import pytest
from minio.deleteobjects import DeleteObject
from psycopg import sql
from psycopg.rows import dict_row

from app.modules.mcp_read.application.wiki_page import (
    ReadWikiPageUseCase,
    WikiPageReadError,
    WikiPageReadLimits,
)
from app.modules.mcp_read.infrastructure.bounded_wiki_page_repository import (
    BoundedWikiPageRepository,
)
from app.modules.wiki_ingestion.infrastructure import object_storage

DSN = os.environ.get("TEST_AGENT_DATABASE_URL") or os.environ.get(
    "CONCEPT_LOCK_TEST_DSN"
)
AI_SCHEMA_SQL = (
    Path(__file__).resolve().parents[3] / "db" / "ai_schema.sql"
).read_text(encoding="utf-8")


class _SlowMinioProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class _SlowMinioHandler(socketserver.BaseRequestHandler):
    target: tuple[str, int]

    def handle(self) -> None:
        with socket.create_connection(self.target, timeout=1) as upstream:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = self.request.recv(8192)
                if not chunk:
                    return
                request += chunk
            upstream.sendall(request)
            response = upstream.recv(65536)
            time.sleep(0.1)
            try:
                self.request.sendall(response)
            except OSError:
                pass


def limits() -> WikiPageReadLimits:
    return WikiPageReadLimits(
        max_id_chars=255,
        max_metadata_bytes=512,
        max_body_bytes=128,
        max_response_bytes=1024,
        deadline_ms=2000,
        db_connect_timeout_ms=500,
        db_statement_timeout_ms=500,
        storage_connect_timeout_ms=500,
        storage_read_timeout_ms=500,
    )


@pytest.mark.skipif(not DSN, reason="isolated PostgreSQL test URL is not configured")
def test_bounded_page_read_with_isolated_postgres_and_minio(monkeypatch) -> None:
    schema = "test_mcp_wiki_page_" + uuid4().hex
    bucket = "mcp-page-" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in DSN else "?"
    scoped = f"{DSN}{separator}options={quote(f'-csearch_path={schema}')}"
    monkeypatch.setenv("S3_BUCKET", bucket)
    storage = object_storage.client()
    storage.make_bucket(bucket)

    def connect(timeout_ms: int):
        return psycopg.connect(
            scoped,
            row_factory=dict_row,
            connect_timeout=max(1, timeout_ms // 1000 or 1),
        )

    def put(key: str, body: bytes) -> str:
        storage.put_object(
            bucket, key, BytesIO(body), len(body), content_type="text/markdown"
        )
        return f"s3://{bucket}/{key}"

    def insert(
        page_id: str,
        workspace_id: str,
        status: str,
        uri: str,
        *,
        title: str | None = None,
    ) -> None:
        with psycopg.connect(scoped) as conn:
            conn.execute(
                """
                INSERT INTO wiki_pages
                    (id, page_type, title, slug, markdown_uri, user_id, workspace_id, status, created_at, updated_at)
                VALUES (%s, 'concept', %s, %s, %s, 'user-a', %s, %s, now(), now())
                """,
                (
                    page_id,
                    title or f"Title {page_id}",
                    page_id,
                    uri,
                    workspace_id,
                    status,
                ),
            )

    try:
        with psycopg.connect(scoped) as conn:
            conn.execute(AI_SCHEMA_SQL)

        exact = "# 제목\n\n유니코드🙂\ntrailing  \n".encode()
        insert("page-target", "workspace-target", "active", put("target.md", exact))
        insert("page-empty", "workspace-target", "active", put("empty.md", b""))
        insert("page-other", "workspace-other", "active", put("other.md", b"other"))
        insert("page-draft", "workspace-target", "draft", put("draft.md", b"draft"))
        insert("page-large", "workspace-target", "active", put("large.md", b"x" * 129))
        insert(
            "page-metadata-large",
            "workspace-target",
            "active",
            put("metadata-large.md", b"body"),
            title="x" * 513,
        )
        insert("page-invalid", "workspace-target", "active", put("invalid.md", b"\xff"))
        insert(
            "page-missing", "workspace-target", "active", f"s3://{bucket}/missing.md"
        )

        repository = BoundedWikiPageRepository(connect=connect)
        use_case = ReadWikiPageUseCase(repository, limits())
        response = use_case.execute(
            workspace_id="workspace-target", page_id="page-target"
        )
        assert response["body"] == exact.decode()
        assert response["content_hash"] == f"sha256:{hashlib.sha256(exact).hexdigest()}"

        empty = use_case.execute(workspace_id="workspace-target", page_id="page-empty")
        assert empty["body"] == ""
        assert empty["content_hash"] == (
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

        for workspace_id, page_id in (
            ("workspace-target", "page-other"),
            ("workspace-target", "page-draft"),
        ):
            with pytest.raises(WikiPageReadError) as hidden:
                use_case.execute(workspace_id=workspace_id, page_id=page_id)
            assert hidden.value.code == "PAGE_NOT_FOUND"

        for page_id, code in (
            ("page-large", "RESPONSE_LIMIT_EXCEEDED"),
            ("page-metadata-large", "RESPONSE_LIMIT_EXCEEDED"),
            ("page-invalid", "DEPENDENCY_UNAVAILABLE"),
            ("page-missing", "DEPENDENCY_UNAVAILABLE"),
        ):
            with pytest.raises(WikiPageReadError) as failed:
                use_case.execute(workspace_id="workspace-target", page_id=page_id)
            assert failed.value.code == code

        endpoint = urlparse(os.environ["S3_ENDPOINT"])
        _SlowMinioHandler.target = (endpoint.hostname or "minio", endpoint.port or 9000)
        with _SlowMinioProxy(("127.0.0.1", 0), _SlowMinioHandler) as proxy:
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            monkeypatch.setenv(
                "S3_ENDPOINT", f"http://127.0.0.1:{proxy.server_address[1]}"
            )
            with pytest.raises(WikiPageReadError) as timed_out:
                ReadWikiPageUseCase(
                    BoundedWikiPageRepository(connect=connect),
                    replace(limits(), storage_read_timeout_ms=10),
                ).execute(workspace_id="workspace-target", page_id="page-target")
            assert (timed_out.value.code, timed_out.value.retryable) == (
                "DEPENDENCY_UNAVAILABLE",
                True,
            )
            proxy.shutdown()
            thread.join(timeout=1)
        monkeypatch.setenv("S3_ENDPOINT", endpoint.geturl())

        class StatusChangingRepository(BoundedWikiPageRepository):
            def _read_body(self, uri, configured):
                body = super()._read_body(uri, configured)
                with psycopg.connect(scoped) as conn:
                    conn.execute(
                        "UPDATE wiki_pages SET status = 'deleted' WHERE id = 'page-empty'"
                    )
                return body

        with pytest.raises(WikiPageReadError) as changed:
            ReadWikiPageUseCase(
                StatusChangingRepository(connect=connect), limits()
            ).execute(
                workspace_id="workspace-target",
                page_id="page-empty",
            )
        assert changed.value.code == "PAGE_CHANGED"
    finally:
        errors = storage.remove_objects(
            bucket,
            (
                DeleteObject(item.object_name)
                for item in storage.list_objects(bucket, recursive=True)
            ),
        )
        assert list(errors) == []
        storage.remove_bucket(bucket)
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
