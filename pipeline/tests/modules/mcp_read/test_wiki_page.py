from __future__ import annotations

import hashlib

import pytest

from app.core.pipeline_control import DeadlineExceededError
from app.modules.mcp_read.application.wiki_page import (
    ReadWikiPageUseCase,
    WikiPageBody,
    WikiPageReadError,
    WikiPageReadLimits,
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


class Repository:
    def __init__(
        self, page: WikiPageBody | None = None, error: Exception | None = None
    ) -> None:
        self.page = page
        self.error = error

    def read_active_page(self, **_kwargs):
        if self.error:
            raise self.error
        return self.page


def test_exact_utf8_body_and_hash_are_returned_without_normalization() -> None:
    raw = "# 제목\n\n유니코드🙂\ntrailing  \n".encode()
    response = ReadWikiPageUseCase(
        Repository(WikiPageBody("page-a", "concept", "제목", raw)),
        limits(),
    ).execute(workspace_id="workspace-a", page_id="page-a")

    assert response == {
        "schema_version": "mcp.wiki-page.v1",
        "page_id": "page-a",
        "page_type": "CONCEPT",
        "title": "제목",
        "body": raw.decode(),
        "content_hash": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT",
    }


@pytest.mark.parametrize(
    ("page", "configured", "code"),
    [
        (
            WikiPageBody("page-a", "concept", "title", b"12345"),
            {"max_body_bytes": 4},
            "RESPONSE_LIMIT_EXCEEDED",
        ),
        (
            WikiPageBody("page-a", "concept", "title", b"body"),
            {"max_response_bytes": 20},
            "RESPONSE_LIMIT_EXCEEDED",
        ),
        (
            WikiPageBody("page-a", "concept", "title-too-long", b"body"),
            {"max_metadata_bytes": 4},
            "RESPONSE_LIMIT_EXCEEDED",
        ),
        (
            WikiPageBody("page-a", "concept", "title", b"\xff"),
            {},
            "DEPENDENCY_UNAVAILABLE",
        ),
        (
            WikiPageBody("other", "concept", "title", b"body"),
            {},
            "DEPENDENCY_UNAVAILABLE",
        ),
    ],
)
def test_unsafe_or_over_limit_results_fail_closed(page, configured, code) -> None:
    use_case = ReadWikiPageUseCase(Repository(page), limits(**configured))
    with pytest.raises(WikiPageReadError) as raised:
        use_case.execute(workspace_id="workspace-a", page_id="page-a")
    assert raised.value.code == code


def test_missing_page_and_deadline_use_safe_errors() -> None:
    with pytest.raises(WikiPageReadError) as missing:
        ReadWikiPageUseCase(Repository(), limits()).execute(
            workspace_id="workspace-a", page_id="page-a"
        )
    assert (missing.value.status_code, missing.value.code) == (404, "PAGE_NOT_FOUND")

    with pytest.raises(WikiPageReadError) as timed_out:
        ReadWikiPageUseCase(
            Repository(error=DeadlineExceededError()), limits()
        ).execute(
            workspace_id="workspace-a",
            page_id="page-a",
        )
    assert (timed_out.value.code, timed_out.value.retryable) == (
        "DEPENDENCY_UNAVAILABLE",
        True,
    )


def test_identifiers_are_bounded_before_repository_access() -> None:
    repository = Repository(WikiPageBody("page-a", "concept", "title", b"body"))
    with pytest.raises(WikiPageReadError) as raised:
        ReadWikiPageUseCase(repository, limits(max_id_chars=5)).execute(
            workspace_id="workspace-too-long",
            page_id="page-a",
        )
    assert raised.value.code == "INVALID_INPUT"
