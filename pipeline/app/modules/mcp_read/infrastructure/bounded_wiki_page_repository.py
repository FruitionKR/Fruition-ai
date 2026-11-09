from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import certifi
import psycopg
from minio import Minio
from minio.error import MinioException
from psycopg.rows import dict_row
from urllib3 import PoolManager
from urllib3.exceptions import HTTPError
from urllib3.util import Retry, Timeout

from app.core.ai_database import ai_database_url
from app.modules.mcp_read.application.wiki_page import (
    WikiPageBody,
    WikiPageReadError,
    WikiPageReadLimits,
)
from app.modules.wiki_ingestion.infrastructure import object_storage

_PAGE_SQL = """
SELECT id, page_type,
       CASE WHEN octet_length(title) <= %s THEN title END AS title,
       CASE WHEN octet_length(COALESCE(markdown_uri, '')) <= %s
            THEN COALESCE(markdown_uri, '') END AS markdown_uri,
       status, updated_at,
       octet_length(title) > %s
           OR octet_length(COALESCE(markdown_uri, '')) > %s AS metadata_exceeded
FROM wiki_pages
WHERE id = %s AND workspace_id = %s
"""


def _connect(timeout_ms: int):
    return psycopg.connect(
        ai_database_url(),
        row_factory=dict_row,
        connect_timeout=max(1, math.ceil(timeout_ms / 1000)),
    )


def _storage_client(
    connect_timeout_ms: int, read_timeout_ms: int
) -> tuple[Minio, PoolManager]:
    http = PoolManager(
        timeout=Timeout(connect=connect_timeout_ms / 1000, read=read_timeout_ms / 1000),
        maxsize=10,
        cert_reqs="CERT_REQUIRED",
        ca_certs=certifi.where(),
        retries=Retry(total=0, connect=0, read=0, redirect=0, status=0),
    )
    try:
        client = object_storage.client(http_client=http)
    except (HTTPError, MinioException, OSError, ValueError):
        http.clear()
        raise
    return client, http


class BoundedWikiPageRepository:
    def __init__(
        self,
        *,
        connect: Callable[[int], Any] = _connect,
        storage_client: Callable[
            [int, int], tuple[Minio, PoolManager]
        ] = _storage_client,
    ) -> None:
        self._connect = connect
        self._storage_client = storage_client

    def read_active_page(
        self,
        *,
        workspace_id: str,
        page_id: str,
        limits: WikiPageReadLimits,
    ) -> WikiPageBody | None:
        page = self._read_metadata(
            workspace_id=workspace_id,
            page_id=page_id,
            limits=limits,
            active_only=True,
        )
        if page is None:
            return None
        if page["metadata_exceeded"]:
            raise WikiPageReadError(
                413,
                "RESPONSE_LIMIT_EXCEEDED",
                "Wiki page metadata exceeds the configured limit.",
            )
        uri = str(page["markdown_uri"] or "")
        if not uri:
            raise WikiPageReadError(
                503, "DEPENDENCY_UNAVAILABLE", "Wiki page body is unavailable."
            )

        body = self._read_body(uri, limits)
        current = self._read_metadata(
            workspace_id=workspace_id,
            page_id=page_id,
            limits=limits,
            active_only=False,
        )
        compared_fields = (
            "id",
            "page_type",
            "title",
            "markdown_uri",
            "status",
            "updated_at",
        )
        if (
            current is None
            or current["status"] != "active"
            or any(current[key] != page[key] for key in compared_fields)
        ):
            raise WikiPageReadError(
                409,
                "PAGE_CHANGED",
                "Wiki page changed during the read.",
                retryable=True,
            )
        return WikiPageBody(
            page_id=str(page["id"]),
            page_type=str(page["page_type"]),
            title=str(page["title"]),
            body=body,
        )

    def _read_metadata(
        self,
        *,
        workspace_id: str,
        page_id: str,
        limits: WikiPageReadLimits,
        active_only: bool,
    ) -> dict[str, Any] | None:
        sql = _PAGE_SQL + ("  AND status = 'active'\n" if active_only else "")
        with self._connect(limits.db_connect_timeout_ms) as conn:
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{limits.db_statement_timeout_ms}ms",),
            )
            row = conn.execute(
                sql,
                (
                    limits.max_metadata_bytes,
                    limits.max_metadata_bytes,
                    limits.max_metadata_bytes,
                    limits.max_metadata_bytes,
                    page_id,
                    workspace_id,
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    def _read_body(self, uri: str, limits: WikiPageReadLimits) -> bytes:
        try:
            client, http = self._storage_client(
                limits.storage_connect_timeout_ms,
                limits.storage_read_timeout_ms,
            )
        except (HTTPError, MinioException, OSError, ValueError):
            raise WikiPageReadError(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "Wiki page storage is temporarily unavailable.",
                retryable=True,
            ) from None
        bucket, object_name = object_storage.split_storage_uri(uri)
        try:
            before = client.stat_object(bucket, object_name)
            if before.size is None or before.size > limits.max_body_bytes:
                raise WikiPageReadError(
                    413,
                    "RESPONSE_LIMIT_EXCEEDED",
                    "Wiki page body exceeds the configured limit.",
                )
            if before.size == 0:
                after = client.stat_object(bucket, object_name)
                if after.size != 0 or before.etag != after.etag:
                    raise WikiPageReadError(
                        409,
                        "PAGE_CHANGED",
                        "Wiki page body changed during the read.",
                        retryable=True,
                    )
                return b""
            response = client.get_object(
                bucket, object_name, offset=0, length=limits.max_body_bytes + 1
            )
            try:
                body = response.read(limits.max_body_bytes + 1)
                response_etag = (response.headers.get("etag") or "").strip('"')
            finally:
                try:
                    response.close()
                finally:
                    response.release_conn()
            if len(body) > limits.max_body_bytes:
                raise WikiPageReadError(
                    413,
                    "RESPONSE_LIMIT_EXCEEDED",
                    "Wiki page body exceeds the configured limit.",
                )
            after = client.stat_object(bucket, object_name)
            if (
                len(body) != before.size
                or before.size != after.size
                or before.etag != after.etag
                or (response_etag and before.etag != response_etag)
            ):
                raise WikiPageReadError(
                    409,
                    "PAGE_CHANGED",
                    "Wiki page body changed during the read.",
                    retryable=True,
                )
            return body
        except WikiPageReadError:
            raise
        except (HTTPError, MinioException, OSError, ValueError):
            raise WikiPageReadError(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "Wiki page storage is temporarily unavailable.",
                retryable=True,
            ) from None
        finally:
            http.clear()
