from __future__ import annotations

import json
import os
from contextlib import contextmanager

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.modules.mcp_read.application.wiki_search import (
    SearchWikiUseCase,
    WikiSearchActor,
    WikiSearchError,
    WikiSearchLimits,
)
from app.core.jev_client import JEV_EVIDENCE_ENABLED_ENV, build_jev_client
from app.core.llm_env import int_env
from app.modules.mcp_read.infrastructure.jev_wiki_search_evidence import JevWikiSearchEvidence
from app.modules.mcp_read.infrastructure.web_wiki_search_candidates import WebWikiSearchCandidates
from app.modules.model_usage.infrastructure.usage_ledger import track_call, usage_scope
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.query.infrastructure.jev_evidence_selector import EvidenceCandidate
from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch
from app.modules.wiki_embedding.infrastructure.remote_embedding_model import RemoteEmbeddingModel
from app.modules.mcp_read.interfaces.http.schemas import McpWikiSearchRequest, McpWikiSearchResponse


router = APIRouter(prefix="/internal/mcp/wiki", tags=["mcp-wiki-read"])
_CONFIG_NAMES = (
    "MCP_WIKI_SEARCH_MAX_STRING_CHARS",
    "MCP_WIKI_SEARCH_MAX_PAGE_SIZE",
    "MCP_WIKI_SEARCH_DEADLINE_MS",
    "MCP_WIKI_SEARCH_MAX_REQUEST_BYTES",
    "MCP_WIKI_SEARCH_MAX_RESPONSE_BYTES",
    "MCP_WIKI_SEARCH_MAX_VECTOR_DIMENSIONS",
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
    cursor_key = os.environ["MCP_WIKI_SEARCH_CURSOR_HMAC_KEY"].encode("utf-8")
    if len(cursor_key) < 32:
        raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki search configuration is invalid.")
    return (
        WikiSearchLimits(
            max_string_chars=values["MCP_WIKI_SEARCH_MAX_STRING_CHARS"],
            max_page_size=values["MCP_WIKI_SEARCH_MAX_PAGE_SIZE"],
            deadline_ms=values["MCP_WIKI_SEARCH_DEADLINE_MS"],
            max_request_bytes=values["MCP_WIKI_SEARCH_MAX_REQUEST_BYTES"],
            max_response_bytes=values["MCP_WIKI_SEARCH_MAX_RESPONSE_BYTES"],
            max_vector_dimensions=values["MCP_WIKI_SEARCH_MAX_VECTOR_DIMENSIONS"],
        ),
        cursor_key,
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


class ConfiguredJevWikiSearchEvidence:
    """요청이 `jev_allowed`일 때만 use case가 부른다. 그때 Jev 클라이언트를 만들어 크레딧이 없는 요청은 Jev를 만들지도 않는다."""

    def select(self, actor: WikiSearchActor, query: str, candidates: list[EvidenceCandidate]) -> list[str]:
        # 웹 질의와 같은 설정·키로 Jev 근거 판정을 쓴다. 꺼져 있거나 키가 없으면 하이브리드 순서만 쓴다.
        client = build_jev_client(JEV_EVIDENCE_ENABLED_ENV, interactive=True)
        if client is None:
            return []
        evidence = JevWikiSearchEvidence(client, max_selected=int_env("QUERY_EVIDENCE_LIMIT", 8))
        return evidence.select(actor, query, candidates)


def get_search_use_case() -> SearchWikiUseCase:
    limits, cursor_key = load_search_config()
    if not os.environ.get("EMBEDDING_SERVICE_URL", "").strip():
        raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Remote embedding service is not configured.", retryable=True)
    model = RemoteEmbeddingModel.from_env()
    # 웹 근거 선택(dependencies._build_query_context)과 같은 점수기다. 질문 임베딩은 같은 인스턴스 캐시를 쓴다.
    embedding_search = StoredWikiPageEmbeddingSearch(model).without_live_embedding()
    return SearchWikiUseCase(
        repository=WebWikiSearchCandidates(embedding_search, Bm25Searcher()),
        embedding_search=embedding_search,
        limits=limits,
        cursor_key=cursor_key,
        query_embedding_model=model.model_name,
        track_query_embedding=_query_embedding_usage,
        evidence_selector=ConfiguredJevWikiSearchEvidence(),
    )


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
