from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import Field, StrictBool, StrictInt, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from app.modules.mcp_pdf_conversion import service
from app.modules.mcp_pdf_conversion.service import McpPdfConversionError
from app.modules.mcp_wiki_draft.interfaces.http.schemas import ExactModel, McpWikiActorRequest, bounded_nonblank

router = APIRouter(prefix="/internal/mcp/pdf-conversions", tags=["mcp-pdf-conversion"])
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_DOCUMENT_ID = re.compile(r"(?:doc_|chatdoc_)[A-Za-z0-9][A-Za-z0-9_-]{0,246}")
_SOURCE_KEY = re.compile(r"sources/documents/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}/original")
# 창 결과 하나의 상한. 쪽 본문 하나와 표·수식 몇 개를 담는다.
_MAX_REQUEST_BYTES = 1024 * 1024


class PdfActor(McpWikiActorRequest):
    tool_name: Literal["start_pdf_conversion", "get_conversion_window", "submit_conversion_window",
                       "finalize_pdf_conversion"]


class _Request(ExactModel):
    schema_version: Literal["mcp.pdf-conversion.v1"]
    actor: PdfActor


class _Idempotent(_Request):
    idempotency_key: str
    request_hash: str

    @field_validator("idempotency_key")
    @classmethod
    def key(cls, value: str) -> str:
        return bounded_nonblank(value, "idempotency_key", 255)

    @field_validator("request_hash")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("request_hash must be sha256")
        return value


class StartRequest(_Idempotent):
    document_id: str
    source_key: str
    source_content_hash: str
    page_count: StrictInt = Field(ge=1, le=50)
    describe_figures: StrictBool

    @field_validator("document_id")
    @classmethod
    def document(cls, value: str) -> str:
        if not _DOCUMENT_ID.fullmatch(value):
            raise ValueError("invalid document_id")
        return value

    @field_validator("source_key")
    @classmethod
    def source(cls, value: str) -> str:
        if not _SOURCE_KEY.fullmatch(value):
            raise ValueError("invalid source_key")
        return value

    @field_validator("source_content_hash")
    @classmethod
    def source_hash(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("invalid source_content_hash")
        return value


class StatusRequest(_Request):
    pass


class ReadWindowRequest(_Request):
    window_ordinal: StrictInt = Field(ge=1, le=100_000)


class SubmitRequest(_Idempotent):
    window_hash: str
    result: dict[str, Any]


class FinalizeRequest(_Request):
    pass


def _error(error: McpPdfConversionError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content={"error": {
        "code": error.code, "message": error.message, "retryable": error.retryable, "handle": None, "state": None}})


async def _validated(request: Request, model):
    body = await request.body()
    if len(body) > _MAX_REQUEST_BYTES:
        raise McpPdfConversionError(413, "REQUEST_TOO_LARGE", "Request is too large.")
    try:
        return model.model_validate_json(body)
    except ValidationError:
        raise McpPdfConversionError(400, "INVALID_INPUT", "Request is invalid.") from None


def _actor(payload) -> dict[str, Any]:
    return payload.actor.model_dump()


@router.post("")
async def start(request: Request):
    try:
        p = await _validated(request, StartRequest)
        return await run_in_threadpool(service.create, _actor(p), p.idempotency_key, p.request_hash, p.document_id,
                                       p.source_key, p.source_content_hash, p.page_count, p.describe_figures)
    except McpPdfConversionError as exc:
        return _error(exc)


@router.post("/{conversion_id}/status")
async def status(conversion_id: str, request: Request):
    try:
        p = await _validated(request, StatusRequest)
        return await run_in_threadpool(service.status, service.conversion_uuid(conversion_id), _actor(p))
    except McpPdfConversionError as exc:
        return _error(exc)


@router.post("/{conversion_id}/windows/read")
async def read_window(conversion_id: str, request: Request):
    try:
        p = await _validated(request, ReadWindowRequest)
        return await run_in_threadpool(service.read_window, service.conversion_uuid(conversion_id), _actor(p),
                                       p.window_ordinal)
    except McpPdfConversionError as exc:
        return _error(exc)


@router.post("/{conversion_id}/windows/{window_id}/submissions")
async def submit(conversion_id: str, window_id: str, request: Request):
    try:
        p = await _validated(request, SubmitRequest)
        return await run_in_threadpool(service.submit, service.conversion_uuid(conversion_id), _actor(p),
                                       service.conversion_uuid(window_id), p.window_hash, p.idempotency_key,
                                       p.request_hash, p.result)
    except McpPdfConversionError as exc:
        return _error(exc)


@router.post("/{conversion_id}/finalize")
async def finalize(conversion_id: str, request: Request):
    try:
        p = await _validated(request, FinalizeRequest)
        return await run_in_threadpool(service.finalize, service.conversion_uuid(conversion_id), _actor(p))
    except McpPdfConversionError as exc:
        return _error(exc)
