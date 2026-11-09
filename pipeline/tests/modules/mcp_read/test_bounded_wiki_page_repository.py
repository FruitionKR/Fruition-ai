from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from app.modules.mcp_read.application.wiki_page import (
    WikiPageReadError,
    WikiPageReadLimits,
)
from app.modules.mcp_read.infrastructure.bounded_wiki_page_repository import (
    BoundedWikiPageRepository,
    _storage_client,
)


def limits(**overrides: int) -> WikiPageReadLimits:
    values = {
        "max_id_chars": 255,
        "max_metadata_bytes": 512,
        "max_body_bytes": 1024,
        "max_response_bytes": 2048,
        "deadline_ms": 1000,
        "db_connect_timeout_ms": 100,
        "db_statement_timeout_ms": 100,
        "storage_connect_timeout_ms": 100,
        "storage_read_timeout_ms": 100,
    }
    values.update(overrides)
    return WikiPageReadLimits(**values)


@dataclass
class Stat:
    size: int
    etag: str = "etag-a"


class Response:
    def __init__(self, body: bytes, etag: str = "etag-a") -> None:
        self.body = body
        self.headers = {"etag": f'"{etag}"'}
        self.closed = False
        self.released = False

    def read(self, amount: int) -> bytes:
        return self.body[:amount]

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class Pool:
    def __init__(self) -> None:
        self.cleared = False

    def clear(self) -> None:
        self.cleared = True


class Storage:
    def __init__(
        self,
        body: bytes,
        *,
        size: int | None = None,
        error: Exception | None = None,
        stats: list[Stat] | None = None,
    ) -> None:
        self.response = Response(body)
        self.stat = Stat(len(body) if size is None else size)
        self.stats = stats
        self.stat_calls = 0
        self.error = error
        self.get_calls: list[tuple] = []

    def stat_object(self, *_args):
        if self.error:
            raise self.error
        result = self.stats[self.stat_calls] if self.stats else self.stat
        self.stat_calls += 1
        return result

    def get_object(self, *args, **kwargs):
        self.get_calls.append((*args, kwargs))
        return self.response


class Result:
    def __init__(self, row=None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, row) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql: str, params: tuple):
        self.calls.append((sql, params))
        return Result(None if "set_config" in sql else self.row)


def page(**changes):
    row = {
        "id": "page-a",
        "page_type": "concept",
        "title": "Title",
        "markdown_uri": "s3://wiki/page-a.md",
        "status": "active",
        "updated_at": "2026-11-08T00:00:00Z",
        "metadata_exceeded": False,
    }
    row.update(changes)
    return row


def repository(rows, storage):
    connections = [Connection(row) for row in rows]
    pool = Pool()

    def connect(_timeout):
        return connections.pop(0)

    return BoundedWikiPageRepository(
        connect=connect,
        storage_client=lambda _connect, _read: (storage, pool),
    ), pool


def test_reads_only_bounded_metadata_and_range_then_cleans_up() -> None:
    storage = Storage(b"exact body")
    subject, pool = repository([page(), page()], storage)

    result = subject.read_active_page(
        workspace_id="workspace-a",
        page_id="page-a",
        limits=limits(max_body_bytes=64, max_metadata_bytes=32),
    )

    assert result and result.body == b"exact body"
    assert storage.get_calls == [("wiki", "page-a.md", {"offset": 0, "length": 65})]
    assert storage.response.closed and storage.response.released and pool.cleared


def test_large_object_is_rejected_before_get_or_materialization() -> None:
    storage = Storage(b"", size=65)
    subject, pool = repository([page()], storage)

    with pytest.raises(WikiPageReadError) as raised:
        subject.read_active_page(
            workspace_id="workspace-a",
            page_id="page-a",
            limits=limits(max_body_bytes=64),
        )

    assert raised.value.code == "RESPONSE_LIMIT_EXCEEDED"
    assert storage.get_calls == []
    assert pool.cleared


def test_empty_object_skips_get_and_rechecks_stat() -> None:
    storage = Storage(b"", stats=[Stat(0), Stat(0)])
    subject, pool = repository([page(), page()], storage)

    result = subject.read_active_page(
        workspace_id="workspace-a", page_id="page-a", limits=limits()
    )

    assert result and result.body == b""
    assert storage.stat_calls == 2
    assert storage.get_calls == []
    assert pool.cleared


@pytest.mark.parametrize("after", [Stat(1), Stat(0, "etag-b")])
def test_empty_object_change_is_rejected_without_get(after: Stat) -> None:
    storage = Storage(b"", stats=[Stat(0), after])
    subject, pool = repository([page()], storage)

    with pytest.raises(WikiPageReadError) as raised:
        subject.read_active_page(
            workspace_id="workspace-a", page_id="page-a", limits=limits()
        )

    assert (raised.value.code, raised.value.retryable) == ("PAGE_CHANGED", True)
    assert storage.get_calls == []
    assert pool.cleared


def test_empty_object_still_runs_final_database_check() -> None:
    storage = Storage(b"", stats=[Stat(0), Stat(0)])
    subject, _pool = repository([page(), page(status="deleted")], storage)

    with pytest.raises(WikiPageReadError) as raised:
        subject.read_active_page(
            workspace_id="workspace-a", page_id="page-a", limits=limits()
        )

    assert (raised.value.code, raised.value.retryable) == ("PAGE_CHANGED", True)
    assert storage.stat_calls == 2
    assert storage.get_calls == []


@pytest.mark.parametrize(
    "final",
    [page(status="deleted"), page(markdown_uri="s3://wiki/page-b.md"), None],
)
def test_changed_pointer_or_status_is_rejected(final) -> None:
    subject, _pool = repository([page(), final], Storage(b"body"))
    with pytest.raises(WikiPageReadError) as raised:
        subject.read_active_page(
            workspace_id="workspace-a", page_id="page-a", limits=limits()
        )
    assert (raised.value.code, raised.value.retryable) == ("PAGE_CHANGED", True)


def test_storage_timeout_is_safe_and_pool_is_cleared() -> None:
    subject, pool = repository(
        [page()], Storage(b"", error=TimeoutError("secret endpoint"))
    )
    with pytest.raises(WikiPageReadError) as raised:
        subject.read_active_page(
            workspace_id="workspace-a", page_id="page-a", limits=limits()
        )
    assert (raised.value.code, raised.value.retryable) == (
        "DEPENDENCY_UNAVAILABLE",
        True,
    )
    assert "secret endpoint" not in raised.value.message
    assert pool.cleared


def test_storage_factory_failure_is_safe_and_pool_is_cleared() -> None:
    pool = Pool()
    with (
        patch(
            "app.modules.mcp_read.infrastructure.bounded_wiki_page_repository."
            "PoolManager",
            return_value=pool,
        ),
        patch(
            "app.modules.mcp_read.infrastructure.bounded_wiki_page_repository."
            "object_storage.client",
            side_effect=ValueError("secret endpoint"),
        ),
        pytest.raises(WikiPageReadError) as raised,
    ):
        BoundedWikiPageRepository()._read_body("s3://wiki/page-a.md", limits())

    assert (raised.value.code, raised.value.retryable) == (
        "DEPENDENCY_UNAVAILABLE",
        True,
    )
    assert "secret endpoint" not in raised.value.message
    assert pool.cleared


def test_aws_credential_timeout_is_safe_and_shared_pool_is_cleared() -> None:
    class SlowCredentialsHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            time.sleep(0.5)
            data = json.dumps(
                {
                    "AccessKeyId": "access",
                    "SecretAccessKey": "secret",
                    "Token": "token",
                    "Expiration": "2099-01-01T00:00:00Z",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowCredentialsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            "S3_CREDENTIALS_MODE": "aws",
            "AWS_REGION": "ap-northeast-2",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": (
                f"http://127.0.0.1:{server.server_port}/creds"
            ),
            "S3_ENDPOINT": "http://127.0.0.1:9",
        }
        with patch.dict(os.environ, env, clear=True):
            client, pool = _storage_client(10, 10)
            assert client._http is pool
            assert client._provider._providers[1]._http_client is pool
            subject = BoundedWikiPageRepository(
                storage_client=lambda _connect, _read: (client, pool)
            )
            with patch.object(pool, "clear", wraps=pool.clear) as clear:
                started = time.monotonic()
                with pytest.raises(WikiPageReadError) as raised:
                    subject._read_body("s3://wiki/page-a.md", limits())
                elapsed = time.monotonic() - started

            assert (raised.value.code, raised.value.retryable) == (
                "DEPENDENCY_UNAVAILABLE",
                True,
            )
            assert elapsed < 0.3
            clear.assert_called_once_with()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_metadata_cap_and_missing_uri_fail_before_storage() -> None:
    storage = Storage(b"body")
    subject, _pool = repository([page(metadata_exceeded=True)], storage)
    with pytest.raises(WikiPageReadError) as oversized:
        subject.read_active_page(
            workspace_id="workspace-a", page_id="page-a", limits=limits()
        )
    assert oversized.value.code == "RESPONSE_LIMIT_EXCEEDED"
    assert storage.get_calls == []

    subject, _pool = repository([page(markdown_uri="")], storage)
    with pytest.raises(WikiPageReadError) as missing:
        subject.read_active_page(
            workspace_id="workspace-a", page_id="page-a", limits=limits()
        )
    assert missing.value.code == "DEPENDENCY_UNAVAILABLE"
