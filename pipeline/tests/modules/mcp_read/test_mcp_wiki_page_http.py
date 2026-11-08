from __future__ import annotations

import os
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

import api
from app.modules.mcp_read.application.wiki_page import WikiPageReadError
from app.modules.mcp_read.interfaces.http import routes as mcp_routes
from app.modules.wiki_ingestion.interfaces.http import routes as wiki_routes


class UseCase:
    def __init__(self, result=None, error=None) -> None:
        self.result = result or {
            "schema_version": "mcp.wiki-page.v1",
            "page_id": "page-a",
            "page_type": "CONCEPT",
            "title": "Title",
            "body": "body  \n",
            "content_hash": "sha256:" + "a" * 64,
            "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT",
        }
        self.error = error
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def client():
    return TestClient(api.app)


def test_internal_token_is_checked_before_bounded_read_builder() -> None:
    builder = Mock()
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(api.database, "verify_agent_schema"),
        patch.object(wiki_routes, "get_page_read_use_case", builder),
        client() as test_client,
    ):
        response = test_client.get(
            "/wiki/pages/page-a?workspace_id=workspace-a&mcp_read=true"
        )
    assert response.status_code == 401
    builder.assert_not_called()


def test_bounded_mode_delegates_and_uses_mcp_error_envelope() -> None:
    use_case = UseCase()
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(api.database, "verify_agent_schema"),
        patch.object(wiki_routes, "get_page_read_use_case", return_value=use_case),
        client() as test_client,
    ):
        response = test_client.get(
            "/wiki/pages/page-a?workspace_id=workspace-a&mcp_read=true",
            headers={"X-Internal-Token": "internal-test-token"},
        )
    assert response.status_code == 200
    assert response.json() == use_case.result
    assert use_case.calls == [{"workspace_id": "workspace-a", "page_id": "page-a"}]

    error = WikiPageReadError(
        413, "RESPONSE_LIMIT_EXCEEDED", "Wiki page body exceeds the configured limit."
    )
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(api.database, "verify_agent_schema"),
        patch.object(
            wiki_routes, "get_page_read_use_case", return_value=UseCase(error=error)
        ),
        client() as test_client,
    ):
        failed = test_client.get(
            "/wiki/pages/page-a?workspace_id=workspace-a&mcp_read=true",
            headers={"X-Internal-Token": "internal-test-token"},
        )
    assert failed.status_code == 413
    assert failed.json() == {
        "error": {
            "code": "RESPONSE_LIMIT_EXCEEDED",
            "message": "Wiki page body exceeds the configured limit.",
            "retryable": False,
            "handle": None,
            "state": None,
            "operation_error": None,
        }
    }


def test_legacy_get_keeps_existing_repository_response() -> None:
    legacy = {
        "id": "page-a",
        "markdown": "legacy body",
        "source_documents": [],
        "related_pages": [],
    }
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(api.database, "verify_agent_schema"),
        patch.object(
            wiki_routes.database, "get_wiki_page", return_value=legacy
        ) as existing,
        patch.object(wiki_routes, "get_page_read_use_case") as builder,
        client() as test_client,
    ):
        response = test_client.get(
            "/wiki/pages/page-a?workspace_id=workspace-a",
            headers={"X-Internal-Token": "internal-test-token"},
        )
    assert response.status_code == 200
    assert response.json() == legacy
    existing.assert_called_once_with("workspace-a", "page-a")
    builder.assert_not_called()


def test_page_configuration_is_explicit_positive_and_large_enough(monkeypatch) -> None:
    configured = {
        "MCP_WIKI_PAGE_MAX_ID_CHARS": "255",
        "MCP_WIKI_PAGE_MAX_METADATA_BYTES": "1024",
        "MCP_WIKI_PAGE_MAX_BODY_BYTES": "4096",
        "MCP_WIKI_PAGE_MAX_RESPONSE_BYTES": "8192",
        "MCP_WIKI_PAGE_DEADLINE_MS": "2000",
        "MCP_WIKI_PAGE_DB_CONNECT_TIMEOUT_MS": "200",
        "MCP_WIKI_PAGE_DB_STATEMENT_TIMEOUT_MS": "500",
        "MCP_WIKI_PAGE_STORAGE_CONNECT_TIMEOUT_MS": "200",
        "MCP_WIKI_PAGE_STORAGE_READ_TIMEOUT_MS": "500",
    }
    for name in mcp_routes._PAGE_CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(Exception) as missing:
        mcp_routes.load_page_config()
    assert missing.value.code == "DEPENDENCY_UNAVAILABLE"

    for name, value in configured.items():
        monkeypatch.setenv(name, value)
    assert mcp_routes.load_page_config().max_body_bytes == 4096

    monkeypatch.setenv("MCP_WIKI_PAGE_STORAGE_READ_TIMEOUT_MS", "0")
    with pytest.raises(Exception) as invalid:
        mcp_routes.load_page_config()
    assert invalid.value.code == "DEPENDENCY_UNAVAILABLE"

    monkeypatch.setenv("MCP_WIKI_PAGE_STORAGE_READ_TIMEOUT_MS", "500")
    monkeypatch.setenv("MCP_WIKI_PAGE_MAX_RESPONSE_BYTES", "4096")
    with pytest.raises(Exception) as no_overhead:
        mcp_routes.load_page_config()
    assert no_overhead.value.code == "DEPENDENCY_UNAVAILABLE"
