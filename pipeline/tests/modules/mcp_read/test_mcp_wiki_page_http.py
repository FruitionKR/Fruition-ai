from __future__ import annotations

import os
from contextlib import nullcontext
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from app.modules.mcp_wiki_draft import lint_context
from app.modules.wiki_ingestion.interfaces.http import routes as wiki_routes


def _get(path: str, page: dict, context: dict | None = None):
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(api.database, "verify_agent_schema"),
        patch.object(wiki_routes.database, "get_wiki_page", return_value=page) as read,
        patch.object(wiki_routes.database, "connect", return_value=nullcontext("conn")),
        patch.object(lint_context, "concept_lint_context", return_value=context or {}) as lint,
        TestClient(api.app) as client,
    ):
        response = client.get(path, headers={"X-Internal-Token": "internal-test-token"})
    return response, read, lint


def test_wiki_page_route_has_no_separate_mcp_read_path() -> None:
    # MCP get_wiki_page는 document의 웹 상세 조회(WikiService.findById)를 그대로 쓴다. AI에는 웹 경로 하나만 있다.
    page = {"id": "page-a", "page_type": "concept", "title": "Title", "markdown": "body"}
    context = {"statements": [], "links": [], "lint_target_page_ids": ["page-a"],
               "lint_target_truncated": False, "lint_context_error": None}
    response, read, lint = _get("/wiki/pages/page-a?workspace_id=workspace-a&mcp_read=true", page, context)
    assert response.status_code == 200
    # MCP 조회에만 lint 준비 정보를 덧붙이고 기존 필드는 그대로다.
    assert response.json() == {**page, **context}
    read.assert_called_once_with("workspace-a", "page-a")
    lint.assert_called_once_with("conn", "workspace-a", page)


def test_web_wiki_page_read_does_not_compute_lint_context() -> None:
    page = {"id": "page-a", "page_type": "concept", "title": "Title", "markdown": "body"}
    response, _read, lint = _get("/wiki/pages/page-a?workspace_id=workspace-a", page)
    assert response.status_code == 200
    assert response.json() == page
    lint.assert_not_called()
