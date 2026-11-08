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


def test_session_follows_existing_preprocessing_and_assembles_only_validated_results(connect, monkeypatch):
    storage = Storage()
    monkeypatch.setattr(service, "client", lambda: storage)
    started = service.create(actor(), "key-1", HASH, "doc_pdf", "sources/documents/doc_pdf/original", HASH, 10, False)
    conversion = started["conversion_id"]
    assert (started["state"], started["dispatch_start_page"]) == ("PREPARING", 0)
    with pytest.raises(service.McpPdfConversionError) as conflict:
        service.create(actor(), "key-1", "sha256:" + "b" * 64, "doc_pdf", "sources/documents/doc_pdf/original",
                       HASH, 10, False)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"

    # converter가 기존 전처리로 만든 묶음을 올리고 journal에 결과를 남긴 상태를 재현한다.
    prefix = f"tmp/convert/mcp/ws_1/{conversion}/0/"
    with tempfile.TemporaryDirectory() as directory:
        pdf, out, manifest, detected = _prepared(Path(directory), body="본문 원문\n\nXQ001QX\n")
        windows = external.build_windows(pdf, out, manifest, detected, describe_figures=False)
        for path in out.rglob("*"):
            if path.is_file() and path.suffix != ".pdf":
                storage.objects[prefix + path.relative_to(out).as_posix()] = path.read_bytes()
    with connect() as conn:
        conn.execute("INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, status, result) "
                     "VALUES (%s, 'ws_1', 'user_1', 'convert_batch', 'completed', %s)",
                     (service.run_id(conversion, 0), Jsonb({"page_end": 1, "total_pages": 2, "segments": [
                         {"page_start": 1, "page_end": 1, "prefix": prefix, "window_count": len(windows)}]})))
    # 첫 batch가 끝나면 document가 넣을 다음 batch 시작 쪽을 알려 준다.
    assert service.status(conversion, actor())["dispatch_start_page"] == 1
    with connect() as conn:
        conn.execute("INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, status, result) "
                     "VALUES (%s, 'ws_1', 'user_1', 'convert_batch', 'completed', %s)",
                     (service.run_id(conversion, 1), Jsonb({"page_end": 2, "total_pages": 2, "segments": []})))

    assert service.status(conversion, actor())["state"] == "READY"
    assert service.status(conversion, actor())["dispatch_start_page"] is None
    with pytest.raises(service.McpPdfConversionError):
        service.status(conversion, actor(grant_id="00000000-0000-4000-8000-000000000002"))
    window = service.read_window(conversion, actor(), 1)
    assert window["lane"] == "table" and window["image_keys"][0].startswith(prefix)
    with pytest.raises(service.McpPdfConversionError) as early:
        service.finalize(conversion, actor())
    assert early.value.code == "WINDOWS_NOT_SUBMITTED"

    block = window["payload"]["blocks"][0]["block_id"]
    bad = {"results": [{"block_id": block, "action": "replace", "replacement": "표 아님"}]}
    first = service.submit(conversion, actor(), window["window_id"], window["window_hash"], "s1", HASH, bad)
    assert (first["accepted"], first["rejected_block_ids"], first["attempt"]) == (False, [block], 1)
    good = {"results": [{"block_id": block, "action": "replace", "replacement": "| a | b |\n| --- | --- |\n| 1 | 2 |"}]}
    second = service.submit(conversion, actor(), window["window_id"], window["window_hash"], "s2", HASH, good)
    assert (second["accepted"], second["attempt"]) == (True, 2)
    assert service.submit(conversion, actor(), window["window_id"], window["window_hash"], "s1", HASH, bad) == first

    final = service.finalize(conversion, actor())
    markdown = storage.objects[final["result_key"]].decode("utf-8")
    assert "| a | b |" in markdown and "<!-- page 1 -->" in markdown
    assert final["content_hash"] == "sha256:" + __import__("hashlib").sha256(markdown.encode()).hexdigest()
    assert final["restoration_summary"]["rejected_block_ids"] == []
    assert service.finalize(conversion, actor()) == final
    assert service.finalized_conversion_exists("ws_1", "user_1", "doc_pdf", HASH, final["content_hash"])
    assert not service.finalized_conversion_exists("ws_1", "user_2", "doc_pdf", HASH, final["content_hash"])
