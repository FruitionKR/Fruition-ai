from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from app.core.pipeline_control import (
    DeadlineExceededError,
    ensure_task_active,
    request_deadline,
)
from app.modules.mcp_read.application.wiki_search import WikiSearchError


class WikiPageReadError(WikiSearchError):
    pass


@dataclass(frozen=True)
class WikiPageReadLimits:
    max_id_chars: int
    max_metadata_bytes: int
    max_body_bytes: int
    max_response_bytes: int
    deadline_ms: int
    db_connect_timeout_ms: int
    db_statement_timeout_ms: int
    storage_connect_timeout_ms: int
    storage_read_timeout_ms: int


@dataclass(frozen=True)
class WikiPageBody:
    page_id: str
    page_type: str
    title: str
    body: bytes


class WikiPageReadRepositoryPort(Protocol):
    def read_active_page(
        self,
        *,
        workspace_id: str,
        page_id: str,
        limits: WikiPageReadLimits,
    ) -> WikiPageBody | None: ...


class ReadWikiPageUseCase:
    def __init__(
        self, repository: WikiPageReadRepositoryPort, limits: WikiPageReadLimits
    ) -> None:
        self._repository = repository
        self.limits = limits

    def execute(self, *, workspace_id: str, page_id: str) -> dict[str, object]:
        if (
            not workspace_id.strip()
            or not page_id.strip()
            or len(workspace_id) > self.limits.max_id_chars
            or len(page_id) > self.limits.max_id_chars
        ):
            raise WikiPageReadError(
                400, "INVALID_INPUT", "Wiki page identifiers are invalid."
            )

        try:
            with request_deadline(self.limits.deadline_ms / 1000):
                ensure_task_active()
                page = self._repository.read_active_page(
                    workspace_id=workspace_id,
                    page_id=page_id,
                    limits=self.limits,
                )
                ensure_task_active()
        except WikiPageReadError:
            raise
        except DeadlineExceededError:
            raise WikiPageReadError(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "Wiki page read deadline exceeded.",
                retryable=True,
            ) from None

        if page is None:
            raise WikiPageReadError(404, "PAGE_NOT_FOUND", "Wiki page was not found.")
        if page.page_id != page_id or page.page_type not in {"source", "concept"}:
            raise WikiPageReadError(
                503, "DEPENDENCY_UNAVAILABLE", "Wiki page metadata is invalid."
            )
        if any(
            len(value.encode("utf-8")) > self.limits.max_metadata_bytes
            for value in (page.page_id, page.page_type, page.title)
        ):
            raise WikiPageReadError(
                413,
                "RESPONSE_LIMIT_EXCEEDED",
                "Wiki page metadata exceeds the configured limit.",
            )
        if len(page.body) > self.limits.max_body_bytes:
            raise WikiPageReadError(
                413,
                "RESPONSE_LIMIT_EXCEEDED",
                "Wiki page body exceeds the configured limit.",
            )
        try:
            body = page.body.decode("utf-8")
        except UnicodeDecodeError:
            raise WikiPageReadError(
                503, "DEPENDENCY_UNAVAILABLE", "Wiki page body is not valid UTF-8."
            ) from None

        response: dict[str, object] = {
            "schema_version": "mcp.wiki-page.v1",
            "page_id": page.page_id,
            "page_type": page.page_type.upper(),
            "title": page.title,
            "body": body,
            "content_hash": f"sha256:{hashlib.sha256(page.body).hexdigest()}",
            "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT",
        }
        encoded = json.dumps(
            response, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > self.limits.max_response_bytes:
            raise WikiPageReadError(
                413,
                "RESPONSE_LIMIT_EXCEEDED",
                "Wiki page response exceeds the configured limit.",
            )
        return response
