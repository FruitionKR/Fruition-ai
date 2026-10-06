import hashlib
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.modules.wiki_generation.domain.text_utils import normalize_space
from app.modules.wiki_generation.infrastructure.extract import MarkdownBlockExtractor
from app.modules.wiki_ingestion.domain.source_block_changes import compare_source_blocks
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as repository
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_output_persistence as persistence
from app.modules.wiki_generation.infrastructure.pipeline_log import PipelineLog
from run_lab import _extract_pipeline_source, _source_block_records, _source_content_hash

MARKDOWN = "# 레드블랙트리\n\n균형 규칙은\n다섯 가지다.\n\n```python\nx = [1, 2]\n\ny = 3\n```\n\n- 항목\n"


def _records(markdown: str, existing: list[dict] | None = None) -> list[dict]:
    _document, blocks = MarkdownBlockExtractor().extract_text(markdown, source_path="doc.md")
    if existing is not None:
        blocks = compare_source_blocks(existing, blocks).blocks
    return _source_block_records(blocks, with_line_ranges=True)


def _stored_order(records: list[dict]) -> list[dict]:
    """DB가 position 순으로 돌려주는 이전 블록 목록."""
    return [
        {"block_id": record["block_id"], "text": record["text"]}
        for record in sorted(records, key=lambda record: record["position"])
    ]


def test_records_keep_document_order_and_line_ranges() -> None:
    records = _records(MARKDOWN)
    lines = MARKDOWN.split("\n")

    assert [record["position"] for record in records] == [1, 2, 3, 4]
    assert [record["block_type"] for record in records] == ["heading", "paragraph", "code", "list"]
    assert [(record["line_start"], record["line_end"]) for record in records] == [(1, 1), (3, 4), (6, 10), (12, 12)]
    for record in records:
        source = "\n".join(lines[record["line_start"] - 1 : record["line_end"]])
        assert normalize_space(source) == record["text"]


def test_line_numbers_count_only_newline() -> None:
    """프론트 편집기는 '\\n'으로만 줄을 나눈다. \\u2028은 줄바꿈이 아니다."""
    records = _records("첫 줄 같은 줄\n\n둘째 블록\n")

    assert [(record["line_start"], record["line_end"]) for record in records] == [(1, 1), (3, 3)]


def test_chat_records_have_position_without_line_ranges() -> None:
    _document, blocks = MarkdownBlockExtractor().blocks_from_records(
        [{"block_id": "session_1:pair_1", "text": "Q : 질문\nA : 답변"}],
        text="# Chat",
        source_path="chat.md",
    )

    [record] = _source_block_records(blocks, with_line_ranges=False)

    assert record["position"] == 1
    assert record["line_start"] is None and record["line_end"] is None and record["block_type"] is None


def test_source_content_hash_matches_backend_sha256() -> None:
    assert _source_content_hash(MARKDOWN) == hashlib.sha256(MARKDOWN.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(("expected", "warned"), [("0" * 64, True), (hashlib.sha256(MARKDOWN.encode()).hexdigest(), False)])
def test_warns_when_backend_hash_differs_from_ingested_markdown(tmp_path: Path, caplog, expected: str, warned: bool) -> None:
    with caplog.at_level(logging.WARNING, logger="run_lab"):
        *_rest, computed = _extract_pipeline_source(
            SimpleNamespace(source_document_id="doc-1", source_content_hash=expected, save_debug_json=False),
            input_text=MARKDOWN,
            input_source_name="doc.md",
            input_path=Path("doc.md"),
            out=tmp_path,
            log=PipelineLog(tmp_path / "pipeline.log"),
        )

    assert computed == hashlib.sha256(MARKDOWN.encode()).hexdigest()
    assert ("source_content_hash mismatch" in caplog.text) is warned


def test_chat_regeneration_does_not_warn_on_delta_markdown(tmp_path: Path, caplog) -> None:
    """채팅 재생성은 미편입 문답만 보내고 해시는 문서 전체 기준이라 비교하지 않는다."""
    with caplog.at_level(logging.WARNING, logger="run_lab"):
        _extract_pipeline_source(
            SimpleNamespace(
                source_document_id="chatdoc-1",
                source_content_hash="0" * 64,
                save_debug_json=False,
                input_blocks=[{"block_id": "session_1:pair_9", "text": "Q : 새 질문\nA : 새 답변"}],
            ),
            input_text="# Chat\n\nQ : 새 질문\nA : 새 답변",
            input_source_name="chat.md",
            input_path=Path("chat.md"),
            out=tmp_path,
            log=PipelineLog(tmp_path / "pipeline.log"),
        )

    assert "source_content_hash mismatch" not in caplog.text


def test_reingest_with_prepended_block_keeps_ids_and_follows_new_order() -> None:
    first = _records(MARKDOWN)

    second = _records("# 서문\n\n" + MARKDOWN, existing=_stored_order(first))

    assert [record["block_id"] for record in second] == ["B0005", "B0001", "B0002", "B0003", "B0004"]
    assert [record["position"] for record in second] == [1, 2, 3, 4, 5]
    assert second[1]["line_start"] == 3


def test_repeated_reingest_compares_against_document_order() -> None:
    """영구 ID가 문서 순서와 달라진 뒤에도 재편입 비교는 이전 문서 순서를 기준으로 한다."""
    first = _records(MARKDOWN)
    second = _records("# 서문\n\n" + MARKDOWN, existing=_stored_order(first))
    _document, blocks = MarkdownBlockExtractor().extract_text("# 서문\n\n" + MARKDOWN, source_path="doc.md")

    third = compare_source_blocks(_stored_order(second), blocks)

    assert third.moved_block_ids == []
    assert third.added_block_ids == third.modified_block_ids == third.deleted_block_ids == []


def test_persist_source_blocks_stores_positions_and_snapshot_on_same_connection() -> None:
    conn = Mock()

    persistence._persist_source_blocks(
        conn,
        "doc-1",
        {
            "source_blocks": [
                {"block_id": "B0005", "text": "서문", "position": 1, "line_start": 1, "line_end": 1, "block_type": "heading"},
                {"block_id": "B0001", "text": "본문", "position": 2, "line_start": 3, "line_end": 4, "block_type": "paragraph"},
            ],
            "source_content_hash": "a" * 64,
        },
    )

    inserts = [call.args for call in conn.execute.call_args_list if "INSERT INTO source_blocks" in call.args[0]]
    assert [params for _query, params in inserts] == [
        ("doc-1", "B0005", "서문", 1, 1, 1, "heading"),
        ("doc-1", "B0001", "본문", 2, 3, 4, "paragraph"),
    ]
    query, params = conn.execute.call_args_list[-1].args
    assert "INSERT INTO source_block_snapshots" in query
    assert params == ("doc-1", "a" * 64)


def test_persist_source_blocks_without_hash_clears_snapshot() -> None:
    conn = Mock()

    persistence._persist_source_blocks(conn, "doc-1", {"source_blocks": [{"block_id": "B0001", "text": "본문"}]})

    insert_params = conn.execute.call_args_list[1].args[1]
    assert insert_params == ("doc-1", "B0001", "본문", 1, None, None, None)
    query, params = conn.execute.call_args_list[-1].args
    assert "DELETE FROM source_block_snapshots" in query
    assert params == ("doc-1",)


@pytest.fixture
def ai_database(monkeypatch):
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    schema = "test_blocks_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-csearch_path={schema}")

    try:
        with connect() as conn:
            conn.execute(Path(__file__).resolve().parents[3].joinpath("db/ai_schema.sql").read_text())
            conn.execute(
                "INSERT INTO wiki_pages(id, page_type, title, slug, user_id, workspace_id, status, created_at, updated_at) "
                "VALUES ('page', 'source', '문서', 'doc-1', 'user', 'ws', 'active', now(), now())"
            )
            conn.execute(
                "INSERT INTO document_wiki_links(document_id, wiki_page_id, relation_type, workspace_id, created_at) "
                "VALUES ('doc-1', 'page', 'source_of', 'ws', now())"
            )
        monkeypatch.setattr(repository, "connect_ai", connect)
        yield connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_context_returns_blocks_in_document_order_with_snapshot(ai_database) -> None:
    first = _records(MARKDOWN)
    second = _records("# 서문\n\n" + MARKDOWN, existing=_stored_order(first))
    with ai_database() as conn:
        persistence._persist_source_blocks(conn, "doc-1", {"source_blocks": second, "source_content_hash": "b" * 64})

    context = repository.get_document_wiki_context("doc-1", "ws")

    assert context["source_content_hash"] == "b" * 64
    assert [block["block_id"] for block in context["source_blocks"]] == ["B0005", "B0001", "B0002", "B0003", "B0004"]
    assert context["source_blocks"][1] == {
        "block_id": "B0001", "text": "# 레드블랙트리", "position": 2, "line_start": 3, "line_end": 3, "block_type": "heading",
    }
    assert [block["block_id"] for block in repository.list_source_blocks("doc-1")] == [
        "B0005", "B0001", "B0002", "B0003", "B0004",
    ]


def test_failed_ingest_keeps_previous_blocks_and_snapshot(ai_database) -> None:
    with ai_database() as conn:
        persistence._persist_source_blocks(conn, "doc-1", {"source_blocks": _records(MARKDOWN), "source_content_hash": "c" * 64})

    with pytest.raises(RuntimeError):
        with ai_database() as conn:
            persistence._persist_source_blocks(
                conn, "doc-1", {"source_blocks": _records("# 다른 문서\n"), "source_content_hash": "d" * 64}
            )
            raise RuntimeError("후속 저장 실패")

    context = repository.get_document_wiki_context("doc-1", "ws")
    assert context["source_content_hash"] == "c" * 64
    assert len(context["source_blocks"]) == 4


def test_legacy_rows_without_position_sort_after_positioned_rows(ai_database) -> None:
    with ai_database() as conn:
        conn.execute("INSERT INTO source_blocks(document_id, block_id, text) VALUES ('doc-1', 'B0001', '옛 블록')")
        conn.execute(
            "INSERT INTO source_blocks(document_id, block_id, text, position) VALUES ('doc-1', 'B0009', '새 블록', 1)"
        )

    context = repository.get_document_wiki_context("doc-1", "ws")

    assert [block["block_id"] for block in context["source_blocks"]] == ["B0009", "B0001"]
    assert context["source_blocks"][1]["position"] is None
    assert context["source_content_hash"] is None
