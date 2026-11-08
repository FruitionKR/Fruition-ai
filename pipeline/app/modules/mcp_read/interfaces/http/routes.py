from __future__ import annotations

import json
import os
from contextlib import contextmanager

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.modules.mcp_read.application.wiki_page import ReadWikiPageUseCase, WikiPageReadLimits
from app.modules.mcp_read.application.wiki_search import (
    SearchWikiUseCase,
    WikiSearchActor,
    WikiSearchError,
    WikiSearchLimits,
)
from app.modules.mcp_read.infrastructure.bounded_wiki_page_repository import BoundedWikiPageRepository
from app.modules.mcp_read.infrastructure.postgres_wiki_search_repository import PostgresMcpWikiSearchRepository
from app.modules.model_usage.infrastructure.usage_ledger import track_call, usage_scope
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch
from app.modules.wiki_embedding.infrastructure.remote_embedding_model import RemoteEmbeddingModel
from app.modules.mcp_read.interfaces.http.schemas import McpWikiSearchRequest, McpWikiSearchResponse


router = APIRouter(prefix="/internal/mcp/wiki", tags=["mcp-wiki-read"])
_CONFIG_NAMES = (
    "MCP_WIKI_SEARCH_MAX_STRING_CHARS",
    "MCP_WIKI_SEARCH_MAX_PAGE_SIZE",
    "MCP_WIKI_SEARCH_MAX_CANDIDATES",
    "MCP_WIKI_SEARCH_MAX_CANDIDATE_BYTES",
    "MCP_WIKI_SEARCH_DEADLINE_MS",
    "MCP_WIKI_SEARCH_MAX_REQUEST_BYTES",
    "MCP_WIKI_SEARCH_MAX_RESPONSE_BYTES",
    "MCP_WIKI_SEARCH_MAX_SCAN_PAGES",
    "MCP_WIKI_SEARCH_MAX_SCAN_UNITS",
    "MCP_WIKI_SEARCH_MAX_VECTOR_DIMENSIONS",
)
_PAGE_CONFIG_NAMES = (
    "MCP_WIKI_PAGE_MAX_ID_CHARS",
    "MCP_WIKI_PAGE_MAX_METADATA_BYTES",
    "MCP_WIKI_PAGE_MAX_BODY_BYTES",
    "MCP_WIKI_PAGE_MAX_RESPONSE_BYTES",
    "MCP_WIKI_PAGE_DEADLINE_MS",
    "MCP_WIKI_PAGE_DB_CONNECT_TIMEOUT_MS",
    "MCP_WIKI_PAGE_DB_STATEMENT_TIMEOUT_MS",
    "MCP_WIKI_PAGE_STORAGE_CONNECT_TIMEOUT_MS",
    "MCP_WIKI_PAGE_STORAGE_READ_TIMEOUT_MS",
)


def load_search_config() -> tuple[WikiSearchLimits, bytes]:
    missing = [name for name in (*_CONFIG_NAMES, "MCP_WIKI_SEARCH_CURSOR_HMAC_KEY") if not os.environ.get(name, "").strip()]
    if missing:
        raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki search is not configured.")
    values: dict[str, int] = {}
    for name in _CONFIG_NAMES:
        raw = os.environ[name].strip()
        try:
            value = int(raw)
        except ValueError:
            raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki search configuration is invalid.") from None
        if value <= 0:
            raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki search configuration is invalid.")
        values[name] = value
    if values["MCP_WIKI_SEARCH_MAX_CANDIDATES"] < values["MCP_WIKI_SEARCH_MAX_PAGE_SIZE"]:
        raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki search configuration is invalid.")
    if values["MCP_WIKI_SEARCH_MAX_SCAN_PAGES"] < values["MCP_WIKI_SEARCH_MAX_CANDIDATES"]:
        raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki search configuration is invalid.")
    cursor_key = os.environ["MCP_WIKI_SEARCH_CURSOR_HMAC_KEY"].encode("utf-8")
    if len(cursor_key) < 32:
        raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki search configuration is invalid.")
    return (
        WikiSearchLimits(
            max_string_chars=values["MCP_WIKI_SEARCH_MAX_STRING_CHARS"],
            max_page_size=values["MCP_WIKI_SEARCH_MAX_PAGE_SIZE"],
            max_candidates=values["MCP_WIKI_SEARCH_MAX_CANDIDATES"],
            max_candidate_bytes=values["MCP_WIKI_SEARCH_MAX_CANDIDATE_BYTES"],
            deadline_ms=values["MCP_WIKI_SEARCH_DEADLINE_MS"],
            max_request_bytes=values["MCP_WIKI_SEARCH_MAX_REQUEST_BYTES"],
            max_response_bytes=values["MCP_WIKI_SEARCH_MAX_RESPONSE_BYTES"],
            max_scan_pages=values["MCP_WIKI_SEARCH_MAX_SCAN_PAGES"],
            max_scan_units=values["MCP_WIKI_SEARCH_MAX_SCAN_UNITS"],
            max_vector_dimensions=values["MCP_WIKI_SEARCH_MAX_VECTOR_DIMENSIONS"],
        ),
        cursor_key,
    )


def load_page_config() -> WikiPageReadLimits:
    if any(not os.environ.get(name, "").strip() for name in _PAGE_CONFIG_NAMES):
        raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki page read is not configured.")
    values: dict[str, int] = {}
    for name in _PAGE_CONFIG_NAMES:
        try:
            value = int(os.environ[name].strip())
        except ValueError:
            raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki page read configuration is invalid.") from None
        if value <= 0:
            raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki page read configuration is invalid.")
        values[name] = value
    if values["MCP_WIKI_PAGE_MAX_RESPONSE_BYTES"] <= values["MCP_WIKI_PAGE_MAX_BODY_BYTES"]:
        raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki page read configuration is invalid.")
    return WikiPageReadLimits(
        max_id_chars=values["MCP_WIKI_PAGE_MAX_ID_CHARS"],
        max_metadata_bytes=values["MCP_WIKI_PAGE_MAX_METADATA_BYTES"],
        max_body_bytes=values["MCP_WIKI_PAGE_MAX_BODY_BYTES"],
        max_response_bytes=values["MCP_WIKI_PAGE_MAX_RESPONSE_BYTES"],
        deadline_ms=values["MCP_WIKI_PAGE_DEADLINE_MS"],
        db_connect_timeout_ms=values["MCP_WIKI_PAGE_DB_CONNECT_TIMEOUT_MS"],
        db_statement_timeout_ms=values["MCP_WIKI_PAGE_DB_STATEMENT_TIMEOUT_MS"],
        storage_connect_timeout_ms=values["MCP_WIKI_PAGE_STORAGE_CONNECT_TIMEOUT_MS"],
        storage_read_timeout_ms=values["MCP_WIKI_PAGE_STORAGE_READ_TIMEOUT_MS"],
    )


@contextmanager
def _query_embedding_usage(actor: WikiSearchActor, model: str, query: str):
    command = {
        "run_id": actor.invocation_id,
        "workspace_id": actor.workspace_id,
        "user_id": actor.user_id,
        "kind": "mcp_wiki_search_query_embedding",
    }
    with usage_scope(command), track_call("embedding", model) as receipt:
        yield
        receipt["input_characters"] = len(query)


def get_search_use_case() -> SearchWikiUseCase:
    limits, cursor_key = load_search_config()
    if not os.environ.get("EMBEDDING_SERVICE_URL", "").strip():
        raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Remote embedding service is not configured.", retryable=True)
    model = RemoteEmbeddingModel.from_env()
    embedding_search = StoredWikiPageEmbeddingSearch(model).without_live_embedding()
    return SearchWikiUseCase(
        repository=PostgresMcpWikiSearchRepository(),
        embedding_search=embedding_search,
        text_search=Bm25Searcher(),
        limits=limits,
        cursor_key=cursor_key,
        query_embedding_model=model.model_name,
        track_query_embedding=_query_embedding_usage,
    )


def get_page_read_use_case() -> ReadWikiPageUseCase:
    return ReadWikiPageUseCase(BoundedWikiPageRepository(), load_page_config())


def _error(error: WikiSearchError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
                "handle": None,
                "state": None,
                "operation_error": None,
            }
        },
    )


@router.post("/search", response_model=McpWikiSearchResponse)
async def search_wiki(request: Request):
    try:
        use_case = get_search_use_case()
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > use_case.limits.max_request_bytes:
                raise WikiSearchError(413, "REQUEST_TOO_LARGE", "Wiki search request exceeds the configured limit.")
            body.extend(chunk)
        try:
            payload = McpWikiSearchRequest.model_validate(json.loads(body))
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError):
            raise WikiSearchError(400, "INVALID_INPUT", "Wiki search request is invalid.") from None
        response = await run_in_threadpool(use_case.execute, payload.to_application())
        return McpWikiSearchResponse.model_validate(response).model_dump(mode="json")
    except WikiSearchError as exc:
        return _error(exc)
    except Exception:
        return _error(WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki search is temporarily unavailable.", retryable=True))
