from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from app.modules.wiki_ingestion.interfaces.http import routes as wiki_routes


def test_wiki_page_route_has_no_separate_mcp_read_path() -> None:
    # MCP get_wiki_page는 document의 웹 상세 조회(WikiService.findById)를 그대로 쓴다. AI에는 웹 경로 하나만 있다.
    page = {"id": "page-a", "page_type": "concept", "title": "Title", "markdown": "body"}
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(api.database, "verify_agent_schema"),
        patch.object(wiki_routes.database, "get_wiki_page", return_value=page) as read,
        TestClient(api.app) as client,
    ):
        response = client.get(
            "/wiki/pages/page-a?workspace_id=workspace-a&mcp_read=true",
            headers={"X-Internal-Token": "internal-test-token"},
        )
    assert response.status_code == 200
    assert response.json() == page
    read.assert_called_once_with("workspace-a", "page-a")
