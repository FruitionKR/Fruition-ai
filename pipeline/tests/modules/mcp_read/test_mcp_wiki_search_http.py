from __future__ import annotations

import os
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import api
from app.modules.mcp_read.application.wiki_search import WikiSearchLimits
from app.modules.mcp_read.interfaces.http import routes


PAYLOAD = {
    "schema_version": "mcp.wiki-search.v1",
    "actor": {
        "invocation_id": "123e4567-e89b-42d3-a456-426614174000",
        "grant_id": "123e4567-e89b-42d3-b456-426614174001",
        "user_id": "user-1",
        "workspace_id": "workspace-1",
        "client_id": "https://client.example/cimd.json",
        "policy_generation": 1,
        "origin": "MCP_CLIENT",
        "spec_version": "2025-11-25",
        "tool_name": "search_wiki",
    },
    "query": "query",
    "cursor": None,
    "page_size": 1,
}
RESPONSE = {
    "schema_version": "mcp.wiki-search.v1",
    "results": [],
    "next_cursor": None,
    "truncated": False,
    "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT",
}


def fake_use_case() -> Mock:
    use_case = Mock()
    use_case.limits = WikiSearchLimits(512, 5, 1000, 8192, 8192, 1024)
    use_case.execute.return_value = RESPONSE
    return use_case


def test_internal_token_is_required_before_body_validation() -> None:
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(routes, "get_search_use_case", return_value=fake_use_case()) as builder,
        TestClient(api.app) as client,
    ):
        response = client.post("/internal/mcp/wiki/search", content=b"not-json")

    assert response.status_code == 401
    builder.assert_not_called()


def test_route_rejects_unknown_fields_and_actor_contract_mismatch() -> None:
    use_case = fake_use_case()
    headers = {"X-Internal-Token": "internal-test-token"}
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(routes, "get_search_use_case", return_value=use_case),
        TestClient(api.app) as client,
    ):
        unknown = client.post("/internal/mcp/wiki/search", headers=headers, json={**PAYLOAD, "unknown": True})
        wrong_tool = client.post(
            "/internal/mcp/wiki/search",
            headers=headers,
            json={**PAYLOAD, "actor": {**PAYLOAD["actor"], "tool_name": "get_wiki_page"}},
        )

    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "INVALID_INPUT"
    assert wrong_tool.status_code == 400
    assert wrong_tool.json()["error"]["code"] == "INVALID_INPUT"
    use_case.execute.assert_not_called()


def test_route_rejects_noncanonical_actor_provenance() -> None:
    use_case = fake_use_case()
    headers = {"X-Internal-Token": "internal-test-token"}
    invalid_actors = [
        {**PAYLOAD["actor"], "grant_id": "00000000-0000-0000-0000-000000000002"},
        {**PAYLOAD["actor"], "grant_id": PAYLOAD["actor"]["grant_id"].upper()},
        {**PAYLOAD["actor"], "invocation_id": 42},
        {**PAYLOAD["actor"], "client_id": "http://client.example/cimd.json"},
        {**PAYLOAD["actor"], "client_id": "https://user:pass@client.example/cimd.json"},
        {**PAYLOAD["actor"], "client_id": "https://client.example/cimd.json#fragment"},
        {**PAYLOAD["actor"], "client_id": "https://client.example:invalid/cimd.json"},
        {**PAYLOAD["actor"], "unexpected": "nested"},
    ]
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(routes, "get_search_use_case", return_value=use_case),
        TestClient(api.app) as client,
    ):
        responses = [
            client.post(
                "/internal/mcp/wiki/search",
                headers=headers,
                json={**PAYLOAD, "actor": actor},
            )
            for actor in invalid_actors
        ]

    assert all(response.status_code == 400 for response in responses)
    assert all(response.json()["error"]["code"] == "INVALID_INPUT" for response in responses)
    use_case.execute.assert_not_called()


def test_route_accepts_exact_dto_on_internal_router() -> None:
    use_case = fake_use_case()
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(routes, "get_search_use_case", return_value=use_case),
        TestClient(api.app) as client,
    ):
        response = client.post(
            "/internal/mcp/wiki/search",
            headers={"X-Internal-Token": "internal-test-token"},
            json=PAYLOAD,
        )

    assert response.status_code == 200
    assert response.json() == RESPONSE
    [application_request] = [call.args[0] for call in use_case.execute.call_args_list]
    assert application_request.actor.workspace_id == "workspace-1"
    assert application_request.actor.user_id == "user-1"
    assert application_request.actor.grant_id == PAYLOAD["actor"]["grant_id"]


def test_route_rejects_request_before_materializing_more_than_configured_bytes() -> None:
    use_case = fake_use_case()
    use_case.limits = WikiSearchLimits(512, 5, 1000, 10, 8192, 1024)
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(routes, "get_search_use_case", return_value=use_case),
        TestClient(api.app) as client,
    ):
        response = client.post(
            "/internal/mcp/wiki/search",
            headers={"X-Internal-Token": "internal-test-token"},
            json=PAYLOAD,
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
    use_case.execute.assert_not_called()


def test_missing_search_configuration_fails_closed(monkeypatch) -> None:
    for name in (*routes._CONFIG_NAMES, "MCP_WIKI_SEARCH_CURSOR_HMAC_KEY"):
        monkeypatch.delenv(name, raising=False)

    try:
        routes.load_search_config()
    except Exception as exc:
        assert getattr(exc, "code", None) == "DEPENDENCY_UNAVAILABLE"
    else:
        raise AssertionError("missing MCP wiki search configuration must fail closed")


def test_search_configuration_requires_explicit_positive_values_and_key(monkeypatch) -> None:
    configured = {
        "MCP_WIKI_SEARCH_MAX_STRING_CHARS": "512",
        "MCP_WIKI_SEARCH_MAX_PAGE_SIZE": "5",
        "MCP_WIKI_SEARCH_DEADLINE_MS": "1000",
        "MCP_WIKI_SEARCH_MAX_REQUEST_BYTES": "8192",
        "MCP_WIKI_SEARCH_MAX_RESPONSE_BYTES": "8192",
        "MCP_WIKI_SEARCH_MAX_VECTOR_DIMENSIONS": "1024",
        "MCP_WIKI_SEARCH_CURSOR_HMAC_KEY": "test-cursor-key-that-is-at-least-32-bytes",
    }
    for name, value in configured.items():
        monkeypatch.setenv(name, value)

    config, key = routes.load_search_config()

    assert config.max_page_size == 5
    assert config.max_vector_dimensions == 1024
    assert key == configured["MCP_WIKI_SEARCH_CURSOR_HMAC_KEY"].encode()

    monkeypatch.setenv("MCP_WIKI_SEARCH_MAX_PAGE_SIZE", "0")
    try:
        routes.load_search_config()
    except Exception as exc:
        assert getattr(exc, "code", None) == "DEPENDENCY_UNAVAILABLE"
    else:
        raise AssertionError("nonpositive page size must fail closed")


def _post_search(use_case, payload):
    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(api.database, "ensure_ai_schema"),
        patch.object(routes, "get_search_use_case", return_value=use_case),
        TestClient(api.app) as client,
    ):
        return client.post(
            "/internal/mcp/wiki/search",
            headers={"X-Internal-Token": "internal-test-token"},
            json=payload,
        )


def test_jev_allowed_defaults_to_false_and_passes_explicit_true() -> None:
    missing, allowed = fake_use_case(), fake_use_case()

    assert _post_search(missing, PAYLOAD).status_code == 200
    assert _post_search(allowed, {**PAYLOAD, "jev_allowed": True}).status_code == 200

    assert missing.execute.call_args.args[0].jev_allowed is False
    assert allowed.execute.call_args.args[0].jev_allowed is True


def test_jev_allowed_must_be_a_json_boolean() -> None:
    use_case = fake_use_case()

    response = _post_search(use_case, {**PAYLOAD, "jev_allowed": "true"})

    assert response.status_code == 400
    use_case.execute.assert_not_called()


def test_configured_selector_builds_jev_only_when_called(monkeypatch) -> None:
    built = []
    monkeypatch.setattr(routes, "build_jev_client", lambda *args, **kwargs: built.append(args) or None)
    selector = routes.ConfiguredJevWikiSearchEvidence()

    assert built == []
    assert selector.select(None, "query", []) == []
    assert len(built) == 1
