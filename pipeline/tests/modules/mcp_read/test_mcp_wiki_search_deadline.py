from __future__ import annotations

import asyncio
from contextvars import ContextVar
import os
from threading import Event
import time

import httpx
from unittest.mock import patch

import api
from app.modules.mcp_read.application.wiki_search import (
    SearchWikiUseCase,
    WikiSearchActor,
    WikiSearchError,
    WikiSearchLimits,
    WikiSearchRequest,
)
from app.modules.mcp_read.interfaces.http import routes
from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch
from app.modules.wiki_embedding.infrastructure.remote_embedding_model import RemoteEmbeddingModel


ACTOR = WikiSearchActor(
    invocation_id="123e4567-e89b-42d3-a456-426614174000",
    grant_id="123e4567-e89b-42d3-b456-426614174001",
    user_id="user-1",
    workspace_id="workspace-1",
    client_id="https://client.example/cimd.json",
    policy_generation=1,
    origin="MCP_CLIENT",
    spec_version="2025-11-25",
    tool_name="search_wiki",
)
PAYLOAD = {
    "schema_version": "mcp.wiki-search.v1",
    "actor": ACTOR.__dict__,
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


def _limits(deadline_ms: int = 30) -> WikiSearchLimits:
    return WikiSearchLimits(512, 2, deadline_ms, 8192, 8192, 1024)


class _UnusedRepository:
    def list_candidates(self, **_kwargs):
        raise AssertionError("database must not run after embedding deadline")


class _SlowTimeoutTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.timeout: float | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.timeout = float(request.extensions["timeout"]["read"])
        time.sleep(self.timeout + 0.005)
        raise httpx.ReadTimeout("slow provider", request=request)


def test_remote_embedding_consumes_remaining_deadline_and_fails_closed() -> None:
    transport = _SlowTimeoutTransport()
    model = RemoteEmbeddingModel(
        "https://embedding.example",
        "token",
        model_name="test-model",
        timeout_seconds=10,
        attempts=1,
        transport=transport,
    )
    use_case = SearchWikiUseCase(
        repository=_UnusedRepository(),
        embedding_search=StoredWikiPageEmbeddingSearch(model).without_live_embedding(),
        limits=_limits(),
        cursor_key=b"cursor-test-key-that-is-at-least-32-bytes",
        query_embedding_model="test-model",
    )

    started = time.monotonic()
    try:
        use_case.execute(WikiSearchRequest(ACTOR, "query", None, 1))
    except WikiSearchError as exc:
        assert (exc.status_code, exc.code, exc.retryable) == (503, "DEPENDENCY_UNAVAILABLE", True)
    else:
        raise AssertionError("slow embedding must fail closed")

    assert transport.timeout is not None and 0 < transport.timeout <= 0.03
    assert time.monotonic() - started < 0.25


def test_missing_remote_embedding_transport_fails_closed(monkeypatch) -> None:
    configured = {
        "MCP_WIKI_SEARCH_MAX_STRING_CHARS": "512",
        "MCP_WIKI_SEARCH_MAX_PAGE_SIZE": "2",
        "MCP_WIKI_SEARCH_DEADLINE_MS": "30",
        "MCP_WIKI_SEARCH_MAX_REQUEST_BYTES": "8192",
        "MCP_WIKI_SEARCH_MAX_RESPONSE_BYTES": "8192",
        "MCP_WIKI_SEARCH_MAX_VECTOR_DIMENSIONS": "1024",
        "MCP_WIKI_SEARCH_CURSOR_HMAC_KEY": "test-cursor-key-that-is-at-least-32-bytes",
    }
    for name, value in configured.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("EMBEDDING_SERVICE_URL", raising=False)

    try:
        routes.get_search_use_case()
    except WikiSearchError as exc:
        assert (exc.status_code, exc.code) == (503, "DEPENDENCY_UNAVAILABLE")
    else:
        raise AssertionError("MCP search must not fall back to local inference")


def test_blocking_search_keeps_event_loop_and_context_responsive() -> None:
    started = Event()
    release = Event()
    attribution = ContextVar("mcp_attribution", default=None)
    observed: list[str | None] = []

    class BlockingUseCase:
        limits = _limits(deadline_ms=1000)

        def execute(self, _request):
            observed.append(attribution.get())
            started.set()
            assert release.wait(1)
            return RESPONSE

    async def run() -> None:
        attribution.set("usage-scope-1")
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            search = asyncio.create_task(
                client.post(
                    "/internal/mcp/wiki/search",
                    headers={"X-Internal-Token": "internal-test-token"},
                    json=PAYLOAD,
                )
            )
            assert await asyncio.to_thread(started.wait, 0.5)
            health = await asyncio.wait_for(client.get("/health"), timeout=0.1)
            assert health.json() == {"status": "ok"}
            release.set()
            response = await asyncio.wait_for(search, timeout=0.5)
            assert response.status_code == 200

    with (
        patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "internal-test-token"}),
        patch.object(routes, "get_search_use_case", return_value=BlockingUseCase()),
    ):
        asyncio.run(run())

    assert observed == ["usage-scope-1"]
