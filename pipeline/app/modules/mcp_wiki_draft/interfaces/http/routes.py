from __future__ import annotations

import json
import os
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.modules.mcp_wiki_draft.application import McpWikiDraftError, McpWikiDraftIntake
from app.modules.mcp_wiki_draft.interfaces.http.dependencies import get_mcp_wiki_draft_intake
from app.modules.mcp_wiki_draft.interfaces.http.schemas import (
    FinalizeIngestRequest,
    ReadWindowRequest,
    ResumeIngestRequest,
    StartIngestRequest,
    SubmitLintDraftRequest,
    SubmitWindowRequest,
)


router = APIRouter(prefix="/internal/mcp/wiki", tags=["mcp-wiki-draft"])


def _error(error: McpWikiDraftError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message, "retryable": error.retryable, "handle": None, "state": None}},
    )


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object key")
        value[key] = item
    return value


def _request_limit() -> int:
    try:
        value = int(os.environ.get("MCP_WIKI_DRAFT_MAX_REQUEST_BYTES", ""))
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki draft request limit is not configured.", retryable=True) from None


async def _body(request: Request) -> object:
    limit = _request_limit()
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise McpWikiDraftError(413, "REQUEST_TOO_LARGE", "MCP wiki draft request is too large.")
        body.extend(chunk)
    try:
        return json.loads(
            body,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite number")),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise McpWikiDraftError(400, "INVALID_INPUT", "MCP wiki draft request is invalid JSON.") from None


def _uuid(value: str, field: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError:
        raise McpWikiDraftError(404, "NOT_FOUND", f"{field} not found.") from None
    if str(parsed) != value or parsed.version != 4:
        raise McpWikiDraftError(404, "NOT_FOUND", f"{field} not found.")
    return value


async def _validated(request: Request, model):
    try:
        return model.model_validate(await _body(request))
    except ValidationError:
        raise McpWikiDraftError(400, "INVALID_INPUT", "MCP wiki draft request is invalid.") from None


@router.post("/ingest-runs")
async def start_ingest(request: Request, intake: McpWikiDraftIntake = Depends(get_mcp_wiki_draft_intake)):
    try:
        payload = await _validated(request, StartIngestRequest)
        return await run_in_threadpool(intake.start, payload)
    except McpWikiDraftError as exc:
        return _error(exc)


@router.post("/lint-drafts")
async def submit_lint_draft(
    request: Request,
    intake: McpWikiDraftIntake = Depends(get_mcp_wiki_draft_intake),
):
    try:
        payload = await _validated(request, SubmitLintDraftRequest)
        return await run_in_threadpool(intake.lint, payload)
    except McpWikiDraftError as exc:
        return _error(exc)


@router.post("/ingest-runs/{run_id}/resume")
async def resume_ingest(run_id: str, request: Request, intake: McpWikiDraftIntake = Depends(get_mcp_wiki_draft_intake)):
    try:
        payload = await _validated(request, ResumeIngestRequest)
        result = await run_in_threadpool(intake.resume, _uuid(run_id, "Run"), payload.actor)
        return {key: result[key] for key in (
            "schema_version", "run_id", "draft_id", "state", "snapshot_id", "next_window_ordinal", "submitted_window_count"
        )}
    except McpWikiDraftError as exc:
        return _error(exc)


@router.post("/ingest-runs/{run_id}/windows/read")
async def read_window(run_id: str, request: Request, intake: McpWikiDraftIntake = Depends(get_mcp_wiki_draft_intake)):
    try:
        payload = await _validated(request, ReadWindowRequest)
        return await run_in_threadpool(intake.read_window, _uuid(run_id, "Run"), payload.actor, payload.window_ordinal)
    except McpWikiDraftError as exc:
        return _error(exc)


@router.post("/ingest-runs/{run_id}/windows/{window_id}/submissions")
async def submit_window(
    run_id: str,
    window_id: str,
    request: Request,
    intake: McpWikiDraftIntake = Depends(get_mcp_wiki_draft_intake),
):
    try:
        payload = await _validated(request, SubmitWindowRequest)
        return await run_in_threadpool(
            intake.submit, _uuid(run_id, "Run"), _uuid(window_id, "Window"), payload
        )
    except McpWikiDraftError as exc:
        return _error(exc)


@router.post("/ingest-runs/{run_id}/finalize")
async def finalize_ingest(
    run_id: str,
    request: Request,
    intake: McpWikiDraftIntake = Depends(get_mcp_wiki_draft_intake),
):
    try:
        payload = await _validated(request, FinalizeIngestRequest)
        return await run_in_threadpool(intake.finalize, _uuid(run_id, "Run"), payload)
    except McpWikiDraftError as exc:
        return _error(exc)


@router.get("/drafts/{draft_id}/preview")
async def get_draft_preview(
    draft_id: str,
    draft_version: int = Query(ge=1),
    workspace_id: str = Header(alias="X-MCP-Workspace-Id"),
    user_id: str = Header(alias="X-MCP-User-Id"),
    intake: McpWikiDraftIntake = Depends(get_mcp_wiki_draft_intake),
):
    try:
        return await run_in_threadpool(
            intake.preview, _uuid(draft_id, "Draft"), draft_version, workspace_id, user_id
        )
    except McpWikiDraftError as exc:
        return _error(exc)
