"""TEST_AGENT_DATABASE_URL을 지정하면 격리 스키마에서 MCP PDF 변환 세션을 실제 PostgreSQL로 검증한다.

전처리 묶음은 기존 AnyDoc 준비 단계(prepare, AnyDoc·Heron 실행만 대역)와 창 만들기로 실제로 만든다.
"""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from app.modules.document_restoration.infrastructure import external_restoration as external
from app.modules.mcp_pdf_conversion import service
from tests.modules.document_restoration.test_external_restoration import _prepared

HASH = "sha256:" + "a" * 64


class Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, bucket, key):
        return SimpleNamespace(read=lambda: self.objects[key], close=lambda: None, release_conn=lambda: None)

    def put_object(self, bucket, key, data, length, content_type):
        self.objects[key] = data.read()

    def list_objects(self, bucket, prefix, recursive):
        return [SimpleNamespace(object_name=key) for key in sorted(self.objects) if key.startswith(prefix)]


@pytest.fixture
def connect(monkeypatch):
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    schema = "test_mcp_pdf_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-csearch_path={schema}")

    with connect() as conn:
        conn.execute(Path(__file__).resolve().parents[3].joinpath("db/ai_schema.sql").read_text())
    monkeypatch.setattr(service, "_connect", connect)
    try:
        yield connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def actor(**overrides):
    return {"invocation_id": str(uuid4()), "grant_id": "00000000-0000-4000-8000-000000000001", "user_id": "user_1",
            "workspace_id": "ws_1", "client_id": "https://client.example/mcp", "policy_generation": 2,
            "origin": "MCP_CLIENT", "spec_version": "2025-11-25", "tool_name": "start_pdf_conversion", **overrides}


def _ready(connect, storage):
    """converter가 기존 전처리로 2쪽짜리 PDF의 둘째 쪽 묶음을 올리고 journal에 결과를 남긴 상태를 만든다."""
    started = service.create(actor(), "key-1", HASH, "doc_pdf", "sources/documents/doc_pdf/original", HASH, 10, False)
    conversion = started["conversion_id"]
    assert (started["state"], started["dispatch_start_page"]) == ("PREPARING", 0)
    with pytest.raises(service.McpPdfConversionError) as conflict:
        service.create(actor(), "key-1", "sha256:" + "b" * 64, "doc_pdf", "sources/documents/doc_pdf/original",
                       HASH, 10, False)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with connect() as conn:
        conn.execute("INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, status, result) "
                     "VALUES (%s, 'ws_1', 'user_1', 'convert_batch', 'completed', %s)",
                     (service.run_id(conversion, 0), Jsonb({"page_end": 1, "total_pages": 2, "segments": []})))
    # 첫 batch가 끝나면 document가 넣을 다음 batch 시작 쪽을 알려 준다.
    assert service.status(conversion, actor())["dispatch_start_page"] == 1
    prefix = f"tmp/convert/mcp/ws_1/{conversion}/1/"
    with tempfile.TemporaryDirectory() as directory:
        pdf, out, manifest, detected = _prepared(Path(directory), body="본문 원문\n\nXQ001QX\n")
        windows = external.build_windows(pdf, out, manifest, detected, describe_figures=False)
        for path in out.rglob("*"):
            if path.is_file() and path.suffix != ".pdf":
                storage.objects[prefix + path.relative_to(out).as_posix()] = path.read_bytes()
    with connect() as conn:
        conn.execute("INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, status, result) "
                     "VALUES (%s, 'ws_1', 'user_1', 'convert_batch', 'completed', %s)",
                     (service.run_id(conversion, 1), Jsonb({"page_end": 2, "total_pages": 2, "segments": [
                         {"page_start": 2, "page_end": 2, "prefix": prefix, "window_count": len(windows)}]})))
    return conversion, prefix


def test_session_follows_existing_preprocessing_retry_and_assembly(connect, monkeypatch):
    storage = Storage()
    monkeypatch.setattr(service, "client", lambda: storage)
    conversion, prefix = _ready(connect, storage)

    assert service.status(conversion, actor())["state"] == "READY"
    assert service.status(conversion, actor())["dispatch_start_page"] is None
    with pytest.raises(service.McpPdfConversionError):
        service.status(conversion, actor(grant_id="00000000-0000-4000-8000-000000000002"))
    window = service.read_window(conversion, actor(), 1)
    assert window["lane"] == "table" and window["image_keys"][0].startswith(prefix)
    assert (window["page"], window["window_count"], window["max_attempts"]) == (2, 1, 1)
    with pytest.raises(service.McpPdfConversionError) as early:
        service.finalize(conversion, actor())
    assert early.value.code == "WINDOWS_NOT_SUBMITTED"

    block = window["payload"]["blocks"][0]["block_id"]
    bad = {"results": [{"block_id": block, "action": "replace", "replacement": "표 아님"}]}
    first = service.submit(conversion, actor(), window["window_id"], window["window_hash"], "s1", HASH, bad)
    # 기존 복원처럼 같은 창을 다시 묻지 않고, 떨어진 블록만 한 블록 창으로 한 번 더 묻는다.
    assert (first["accepted"], first["rejected_block_ids"], first["attempt"]) == (False, [block], 1)
    assert (first["new_window_ordinals"], first["window_count"], first["submitted_window_count"]) == ([2], 2, 1)
    with pytest.raises(service.McpPdfConversionError) as exhausted:
        service.submit(conversion, actor(), window["window_id"], window["window_hash"], "s9", HASH, bad)
    assert exhausted.value.code == "ATTEMPTS_EXHAUSTED"
    assert service.submit(conversion, actor(), window["window_id"], window["window_hash"], "s1", HASH, bad) == first

    retry = service.read_window(conversion, actor(), 2)
    assert retry["window_id"] != window["window_id"] and retry["window_count"] == 2
    assert [item["block_id"] for item in retry["payload"]["blocks"]] == [block]
    with pytest.raises(service.McpPdfConversionError) as missing:
        service.finalize(conversion, actor())
    assert missing.value.code == "WINDOWS_NOT_SUBMITTED"
    good = {"results": [{"block_id": block, "action": "replace", "replacement": "| a | b |\n| --- | --- |\n| 1 | 2 |"}]}
    second = service.submit(conversion, actor(), retry["window_id"], retry["window_hash"], "s2", HASH, good)
    assert (second["accepted"], second["new_window_ordinals"], second["submitted_window_count"]) == (True, [], 2)

    final = service.finalize(conversion, actor())
    assert final["result_key"] == f"tmp/mcp-convert/ws_1/{conversion}/final.json"
    stored = storage.objects[final["result_key"]]
    assert final["content_hash"] == "sha256:" + __import__("hashlib").sha256(stored).hexdigest()
    # 쪽 번호는 묶음 안 번호 그대로 두고 시작 쪽을 함께 둔다. 원본 기준 번호는 document가 저장 때 바꾼다.
    [segment] = __import__("json").loads(stored)
    assert segment["page_start"] == 2
    assert "| a | b |" in segment["markdown"] and "<!-- page 1 -->" in segment["markdown"]
    assert final["restoration_summary"]["rejected_block_ids"] == []
    assert service.finalize(conversion, actor()) == final
    assert service.finalized_conversion_exists("ws_1", "user_1", "doc_pdf", HASH, final["content_hash"],
                                               final["result_key"])
    assert not service.finalized_conversion_exists("ws_1", "user_2", "doc_pdf", HASH, final["content_hash"],
                                                   final["result_key"])
    assert not service.finalized_conversion_exists("ws_1", "user_1", "doc_pdf", HASH, final["content_hash"],
                                                   f"tmp/mcp-convert/ws_1/{uuid4()}/final.json")
    # 결과는 tmp/ lifecycle(7일)이 지운다. 승인 계획(1일)이 결과보다 먼저 끝나도록 finalize 뒤 6일까지만 받는다.
    with connect() as conn:
        conn.execute("UPDATE mcp_pdf_conversions SET finalized_at = now() - interval '6 days 1 hour' "
                     "WHERE conversion_id = %s", (conversion,))
    assert not service.finalized_conversion_exists("ws_1", "user_1", "doc_pdf", HASH, final["content_hash"],
                                                   final["result_key"])


def test_preview_markdown_joins_batches_and_shortens_image_bytes():
    result = __import__("json").dumps([
        {"page_start": 1, "markdown": "<!-- page 1 -->\n\n![a](data:image/png;base64,AAAA)\n\n"},
        {"page_start": 3, "markdown": "<!-- page 1 -->\n\nB\n"}])

    assert service.converted_preview_markdown(result) == (
        "<!-- page 1 -->\n\n![a](data:image/png;base64,…)\n\n<!-- page 1 -->\n\nB\n")


def test_finalize_has_no_proposal_size_cap(connect, monkeypatch):
    """조립 결과는 승인 요청에 실리지 않고 저장소에 남으므로 승인 요청 상한(4MiB)과 무관하다."""
    storage = Storage()
    monkeypatch.setattr(service, "client", lambda: storage)
    conversion, _prefix = _ready(connect, storage)
    window = service.read_window(conversion, actor(), 1)
    block = window["payload"]["blocks"][0]["block_id"]
    good = {"results": [{"block_id": block, "action": "replace", "replacement": "| a | b |\n| --- | --- |\n| 1 | 2 |"}]}
    service.submit(conversion, actor(), window["window_id"], window["window_hash"], "s1", HASH, good)
    large = "<!-- page 1 -->\n\n" + "가" * (2 * 1024 * 1024)
    monkeypatch.setattr(service, "assemble_external", lambda *args: (large, {
        "broken_pages": 0, "repaired_pages": 0, "fallback_pages": [], "rejected_block_ids": []}))

    final = service.finalize(conversion, actor())

    assert len(storage.objects[final["result_key"]]) > 4 * 1024 * 1024
