"""MCP 외부 AI PDF 변환 세션.

기존 변환 파이프라인을 그대로 따른다. 전처리(AnyDoc crop-first 준비)는 converter worker가 기존 batch·journal로 하고,
기존 selective repair·그림 설명이 공급사에 보내던 요청을 창으로 외부 AI에 준다. 외부 AI 결과는 기존 검증을 거쳐
기존 조립으로 Markdown이 된다. 이 모듈은 공급사를 부르지 않는다.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.ai_database import ai_database_url
from app.core.ai_markdown_sanitizer import external_urls, sanitize_ai_markdown
from app.modules.document_restoration.infrastructure.crop_first_with_anydoc import embed_local_image_links
from app.modules.document_restoration.infrastructure.external_restoration import (
    WINDOWS_FILE,
    assemble_external,
    load_context,
    validate_result,
)
from app.modules.wiki_ingestion.infrastructure.object_storage import bucket_name, client

SCHEMA = "mcp.pdf-conversion.v1"
# 기존 복원의 "한 번 묻고 떨어진 블록만 한 번 더"와 같은 시도 수다.
MAX_ATTEMPTS = 2
# 조립 결과는 document propose_plan artifact로 승인에 들어간다. 그 요청 상한(McpPlanProposalService 4MiB)과 같다.
MAX_MARKDOWN_BYTES = 4 * 1024 * 1024
MANIFEST = Path("layout") / "auto" / "batch.crop_first_manifest.json"
DETECTED = Path("final") / "batch.detected.md"
_PAGE = re.compile(r"<!-- page (\d{1,6}) -->")


class McpPdfConversionError(Exception):
    def __init__(self, status_code: int, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message, self.retryable = status_code, code, message, retryable


def _connect():
    return psycopg.connect(ai_database_url(), row_factory=dict_row)


def _not_found() -> McpPdfConversionError:
    return McpPdfConversionError(404, "RESOURCE_NOT_FOUND", "Conversion not found.")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def conversion_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise _not_found() from None
    if str(parsed) != value:
        raise _not_found()
    return value


def run_id(conversion_id: str, start: int) -> str:
    return f"mcpconvert:{conversion_id}:{start}"


def _owned(conn, conversion_id: str, actor: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM mcp_pdf_conversions WHERE conversion_id = %s", (conversion_id,)).fetchone()
    if row is None or any(str(row[key]) != str(actor[key]) for key in
                          ("workspace_id", "user_id", "grant_id", "client_id", "policy_generation")):
        raise _not_found()
    return row


def create(actor: dict[str, Any], idempotency_key: str, request_hash: str, document_id: str, source_key: str,
           source_content_hash: str, page_count: int, describe_figures: bool) -> dict[str, Any]:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO mcp_pdf_conversions(conversion_id, workspace_id, user_id, grant_id, client_id, policy_generation, "
            "idempotency_key, request_hash, document_id, source_key, source_content_hash, page_count, describe_figures) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (workspace_id, user_id, grant_id, client_id, idempotency_key) DO NOTHING",
            (str(uuid.uuid4()), actor["workspace_id"], actor["user_id"], actor["grant_id"], actor["client_id"],
             actor["policy_generation"], idempotency_key, request_hash, document_id, source_key,
             source_content_hash, page_count, describe_figures))
        row = conn.execute(
            "SELECT * FROM mcp_pdf_conversions WHERE workspace_id=%s AND user_id=%s AND grant_id=%s AND client_id=%s "
            "AND idempotency_key=%s",
            (actor["workspace_id"], actor["user_id"], actor["grant_id"], actor["client_id"], idempotency_key)).fetchone()
        if row["request_hash"] != request_hash:
            raise McpPdfConversionError(409, "IDEMPOTENCY_CONFLICT", "Idempotency key was used for another request.")
        return _status(conn, row)


def status(conversion_id: str, actor: dict[str, Any]) -> dict[str, Any]:
    with _connect() as conn:
        return _status(conn, _owned(conn, conversion_id, actor))


def _batches(conn, row: dict[str, Any]) -> tuple[str, list[dict[str, Any]], int | None, str | None, bool]:
    """converter journal의 batch를 앞에서부터 따라가며 (상태, 준비된 묶음, 전체 쪽수, 실패 코드, 첫 batch 미등록)이다."""
    segments: list[dict[str, Any]] = []
    start, total = 0, None
    while True:
        task = conn.execute("SELECT status, result, error_code FROM ai_task_runs WHERE id = %s",
                            (run_id(str(row["conversion_id"]), start),)).fetchone()
        if task is None:
            return "PREPARING", segments, total, None, start == 0
        if task["status"] == "failed" or task["status"] in {"cancelled", "rollback_failed"}:
            return "FAILED", segments, total, task["error_code"] or "task_failed", False
        if task["status"] != "completed":
            return "PREPARING", segments, total, None, False
        result = task["result"] or {}
        segments.extend(result.get("segments") or [])
        total = int(result["total_pages"])
        start = int(result["page_end"])
        if start >= total:
            return "READY", segments, total, None, False


def _status(conn, row: dict[str, Any]) -> dict[str, Any]:
    state, segments, total, error, needs_dispatch = _batches(conn, row)
    if row["final_markdown_hash"] is not None:
        state = "FINALIZED"
    windows = sum(int(segment["window_count"]) for segment in segments) if state in {"READY", "FINALIZED"} else None
    return {
        "schema_version": SCHEMA, "conversion_id": str(row["conversion_id"]), "state": state,
        "source_document_id": row["document_id"], "source_content_hash": row["source_content_hash"],
        "total_pages": total, "prepared_pages": max((int(s["page_end"]) for s in segments), default=0),
        "window_count": windows, "error_code": error,
        # document가 첫 batch command를 다시 넣어야 하는지. 이후 batch는 converter가 잇는다.
        "needs_dispatch": needs_dispatch,
        "first_batch": {"run_id": run_id(str(row["conversion_id"]), 0), "source_key": row["source_key"],
                        "page_count": row["page_count"], "describe_figures": row["describe_figures"]},
    }


def _read(storage, key: str) -> bytes:
    response = storage.get_object(bucket_name(), key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def _windows(conn, row: dict[str, Any]) -> list[dict[str, Any]]:
    state, segments, _total, _error, _dispatch = _batches(conn, row)
    if state != "READY":
        raise McpPdfConversionError(409, "CONVERSION_NOT_READY", "Preprocessing is not finished.", retryable=True)
    storage, windows = client(), []
    for segment in segments:
        prefix = str(segment["prefix"])
        if not prefix.startswith(f"tmp/convert/mcp/{row['workspace_id']}/{row['conversion_id']}/"):
            raise McpPdfConversionError(503, "DEPENDENCY_UNAVAILABLE", "Preprocessed bundle is invalid.")
        for index, window in enumerate(json.loads(_read(storage, prefix + WINDOWS_FILE.as_posix()))):
            window_id = str(uuid.uuid5(uuid.UUID(str(row["conversion_id"])), f"{prefix}{index}"))
            windows.append({**window, "window_id": window_id, "segment": segment,
                            "window_hash": _sha256(_canonical({"prefix": prefix, "index": index, "window": window}))})
    return windows


def read_window(conversion_id: str, actor: dict[str, Any], ordinal: int) -> dict[str, Any]:
    with _connect() as conn:
        row = _owned(conn, conversion_id, actor)
        windows = _windows(conn, row)
        if not 1 <= ordinal <= len(windows):
            raise _not_found()
        window = windows[ordinal - 1]
        attempts = conn.execute("SELECT count(*) AS n FROM mcp_pdf_conversion_submissions "
                                "WHERE conversion_id = %s AND window_id = %s", (conversion_id, window["window_id"])).fetchone()["n"]
    segment = window["segment"]
    return {
        "schema_version": SCHEMA, "conversion_id": conversion_id, "window_id": window["window_id"],
        "window_ordinal": ordinal, "window_count": len(windows), "window_hash": window["window_hash"],
        "kind": window["kind"], "lane": window["lane"],
        "page": int(segment["page_start"]) - 1 + int(window["page"]),
        "instructions": window["instructions"], "payload": window["payload"],
        "result_schema": window["result_schema"],
        "image_keys": [str(segment["prefix"]) + image for image in window["images"]],
        "submitted_attempts": attempts, "max_attempts": MAX_ATTEMPTS, "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT",
    }


def submit(conversion_id: str, actor: dict[str, Any], window_id: str, window_hash: str, idempotency_key: str,
           request_hash: str, result: Any) -> dict[str, Any]:
    with _connect() as conn:
        row = _owned(conn, conversion_id, actor)
        replay = conn.execute("SELECT request_hash, response FROM mcp_pdf_conversion_submissions "
                              "WHERE conversion_id = %s AND idempotency_key = %s", (conversion_id, idempotency_key)).fetchone()
        if replay is not None:
            if replay["request_hash"] != request_hash:
                raise McpPdfConversionError(409, "IDEMPOTENCY_CONFLICT", "Idempotency key was used for another request.")
            return replay["response"]
        if row["final_markdown_hash"] is not None:
            raise McpPdfConversionError(409, "CONVERSION_FINALIZED", "Conversion is already finalized.")
        windows = _windows(conn, row)
        window = next((item for item in windows if item["window_id"] == window_id), None)
        if window is None:
            raise _not_found()
        if window["window_hash"] != window_hash:
            raise McpPdfConversionError(409, "STALE_WINDOW", "Window changed.")
        conn.execute("SELECT 1 FROM mcp_pdf_conversions WHERE conversion_id = %s FOR UPDATE", (conversion_id,))
        attempt = 1 + conn.execute("SELECT count(*) AS n FROM mcp_pdf_conversion_submissions "
                                   "WHERE conversion_id = %s AND window_id = %s", (conversion_id, window_id)).fetchone()["n"]
        if attempt > MAX_ATTEMPTS:
            raise McpPdfConversionError(409, "ATTEMPTS_EXHAUSTED", "This window has no attempts left.")
        with tempfile.TemporaryDirectory(prefix="fruition-mcp-pdf-") as directory:
            root, storage, prefix = Path(directory), client(), str(window["segment"]["prefix"])
            for relative in (MANIFEST, DETECTED):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_bytes(_read(storage, prefix + relative.as_posix()))
            blocks, fragments = load_context(root / MANIFEST, root / DETECTED)
            try:
                rejected = validate_result(window, result, blocks, fragments)
            except ValueError as exc:
                raise McpPdfConversionError(400, "INVALID_INPUT", str(exc)) from None
        submitted = conn.execute("SELECT count(DISTINCT window_id) AS n FROM mcp_pdf_conversion_submissions "
                                 "WHERE conversion_id = %s", (conversion_id,)).fetchone()["n"] + (attempt == 1)
        response = {
            "schema_version": SCHEMA, "conversion_id": conversion_id, "window_id": window_id, "attempt": attempt,
            "max_attempts": MAX_ATTEMPTS, "accepted": not rejected, "rejected_block_ids": rejected,
            "submitted_window_count": submitted, "window_count": len(windows),
        }
        conn.execute("INSERT INTO mcp_pdf_conversion_submissions(conversion_id, window_id, attempt, window_hash, "
                     "idempotency_key, request_hash, result, response) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                     (conversion_id, window_id, attempt, window_hash, idempotency_key, request_hash,
                      Jsonb(result), Jsonb(response)))
        return response


def renumber_pages(markdown: str, first_page: int) -> str:
    """document ConvertedMarkdownChunks.renumberPages와 같다. 묶음 안 쪽 번호(1부터)를 원본 쪽 번호로 바꾼다."""
    return _PAGE.sub(lambda match: f"<!-- page {int(match.group(1)) + first_page - 1} -->", markdown)


def finalize(conversion_id: str, actor: dict[str, Any]) -> dict[str, Any]:
    """모든 창이 한 번 이상 제출됐을 때 기존 조립으로 Markdown을 만든다. 같은 세션은 같은 결과를 다시 돌려준다."""
    with _connect() as conn:
        conn.execute("SELECT 1 FROM mcp_pdf_conversions WHERE conversion_id = %s FOR UPDATE", (conversion_id,))
        row = _owned(conn, conversion_id, actor)
        if row["final_response"] is not None:
            return row["final_response"]
        windows = _windows(conn, row)
        latest = {str(item["window_id"]): item["result"] for item in conn.execute(
            "SELECT DISTINCT ON (window_id) window_id, result FROM mcp_pdf_conversion_submissions "
            "WHERE conversion_id = %s ORDER BY window_id, attempt DESC", (conversion_id,)).fetchall()}
        missing = [window["window_id"] for window in windows if window["window_id"] not in latest]
        if missing:
            raise McpPdfConversionError(409, "WINDOWS_NOT_SUBMITTED",
                                        f"{len(missing)} windows have no submission.")
        storage, chunks, keep, rejected = client(), [], set(), []
        summary = {"broken_pages": 0, "repaired_pages": 0, "fallback_pages": []}
        # 창이 없는 묶음(복원할 블록이 없는 쪽)도 AnyDoc 본문 그대로 조립한다.
        _state, segments, *_rest = _batches(conn, row)
        for segment in segments:
            prefix, first = str(segment["prefix"]), int(segment["page_start"])
            with tempfile.TemporaryDirectory(prefix="fruition-mcp-pdf-") as directory:
                root = Path(directory)
                for item in storage.list_objects(bucket_name(), prefix=prefix, recursive=True):
                    target = root / item.object_name[len(prefix):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(_read(storage, item.object_name))
                results = [(w, latest[w["window_id"]]) for w in windows if w["segment"]["prefix"] == prefix]
                markdown, part = assemble_external(root, root / MANIFEST, root / DETECTED, results)
                keep |= external_urls((root / DETECTED).read_text(encoding="utf-8"))
                markdown = embed_local_image_links(markdown, root / "final" / "external.restored.md", root)
            chunks.append(renumber_pages(markdown, first).strip())
            summary["broken_pages"] += part["broken_pages"]
            summary["repaired_pages"] += part["repaired_pages"]
            summary["fallback_pages"] += [{**page, "page": first - 1 + int(page["page"])} for page in part["fallback_pages"]]
            rejected += part["rejected_block_ids"]
        # 원문(AnyDoc 추출)에 있던 링크만 남기고, 외부 AI가 새로 넣은 외부 링크·이미지는 글자로 바꾼다.
        markdown = sanitize_ai_markdown("\n\n".join(chunks).strip() + "\n", keep_urls=frozenset(keep))
        if len(markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
            raise McpPdfConversionError(413, "SOURCE_LIMIT_EXCEEDED", "Converted Markdown exceeds the proposal limit.")
        content_hash = _sha256(markdown.encode("utf-8"))
        # 기존 변환 결과처럼 Markdown은 tmp/convert/에 두고 key만 돌려준다(이미지 data: URI로 응답 한도를 넘는다).
        result_key = f"tmp/convert/mcp/{row['workspace_id']}/{conversion_id}/final.md"
        data = markdown.encode("utf-8")
        storage.put_object(bucket_name(), result_key, io.BytesIO(data), length=len(data),
                           content_type="text/markdown; charset=utf-8")
        response = {
            "schema_version": SCHEMA, "conversion_id": conversion_id, "source_document_id": row["document_id"],
            "source_content_hash": row["source_content_hash"], "content_hash": content_hash, "result_key": result_key,
            "restoration_summary": {**summary, "rejected_block_ids": rejected},
        }
        conn.execute("UPDATE mcp_pdf_conversions SET final_markdown_hash = %s, final_response = %s, finalized_at = now() "
                     "WHERE conversion_id = %s", (content_hash, Jsonb(response), conversion_id))
        return response


def finalized_conversion_exists(workspace_id: str, user_id: str, document_id: str, source_content_hash: str,
                                content_hash: str) -> bool:
    """propose_plan의 원본 연결 create_document가 이 사용자의 조립 결과 그대로인지."""
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM mcp_pdf_conversions WHERE workspace_id=%s AND user_id=%s AND document_id=%s "
            "AND source_content_hash=%s AND final_markdown_hash=%s",
            (workspace_id, user_id, document_id, source_content_hash, content_hash)).fetchone() is not None
