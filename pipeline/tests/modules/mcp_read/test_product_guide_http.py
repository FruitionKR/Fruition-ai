import os
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from app.core.product_guide import load_product_guide
from app.modules.agent.infrastructure.chat_completions_conversation_replier import load_product_help_prompt


def test_public_guide_requires_internal_auth_and_matches_help_source():
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "guide-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(api.database, "verify_agent_schema"),
        TestClient(api.app) as client,
    ):
        assert client.get("/internal/mcp/product-guide").status_code == 401
        assert client.get("/internal/mcp/product-guide", headers={"X-Internal-Token": "wrong"}).status_code == 401
        response = client.get("/internal/mcp/product-guide", headers={"X-Internal-Token": "guide-test-token"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"schema_version", "title", "markdown"}
    assert body["schema_version"] == "mcp.product-guide.v1"
    assert body["markdown"] == load_product_guide()
    assert load_product_help_prompt().endswith(body["markdown"])
    assert "## MCP로 위키 편입하기" in body["markdown"]
    assert "get_product_guide" in body["markdown"]
    assert "## 화면 18" in body["markdown"]
    assert 'Return one JSON object' not in body["markdown"]
    assert 'Every payload field is untrusted' not in body["markdown"]
