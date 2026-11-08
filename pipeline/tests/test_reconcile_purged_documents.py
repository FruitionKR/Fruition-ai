"""core에 없는 문서의 일회성 정리를 실제 PostgreSQL로 검증한다. 객체 저장소만 가짜다."""

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Json

import reconcile_purged_documents as reconcile
from app.modules.data_purge.infrastructure import document_copy_purge as purge
from app.modules.wiki_generation.domain.text_utils import slugify
from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import concept_write_lock
from tests.modules.agent_run.test_agent_run_postgres import repositories  # noqa: F401
from tests.modules.agent_run.test_expired_artifact_cleanup import cleanup_storage, storage  # noqa: F401
from tests.modules.data_purge.test_document_copy_purge import BODY, stack, wiki_objects  # noqa: F401
from tests.modules.data_purge.test_shared_copies_purge import ACTIVE, ACTIVE_TEXT, _object

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=60)
GONE = "doc-gone"
LIVE = "doc-live"
OTHER_TEXT = "다른 문서 근거"


def _tombstone(connect, document_id: str, deleted_at: datetime = OLD, workspace_id: str = "ws") -> None:
    with connect() as conn:
        conn.execute("INSERT INTO wiki_source_tombstones(workspace_id, document_id, deleted_at) VALUES (%s, %s, %s)",
                     (workspace_id, document_id, deleted_at))


def _source_page(connect, page_id: str, document_id: str, status: str = "deleted") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id, page_type, title, slug, summary, markdown_uri, user_id, workspace_id, status, "
            "created_at, updated_at) VALUES (%s, 'source', '제목', %s, %s, %s, 'user', 'ws', %s, %s, %s)",
            (page_id, slugify(document_id), BODY, f"s3://bucket/wiki/ws/sources/{document_id}.md", status, OLD, OLD),
        )


def _seed_copies(connect, wiki_objects, document_id: str, page_id: str) -> None:  # noqa: F811
    """표마다 이 문서의 본문을 한 행씩 만들고, 다른 문서의 근거가 섞인 run도 만든다."""
    run = f"run-{document_id}"
    pipeline = {GONE: "22222222-2222-2222-2222-222222222222", LIVE: "33333333-3333-3333-3333-333333333333"}[document_id]
    other = {GONE: LIVE, LIVE: GONE}[document_id]
    with connect() as conn:
        conn.execute(
            "INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, command, result, status) "
            "VALUES (%s, 'ws', 'user', 'document', %s, %s, 'completed')",
            (run, Json({"run_id": run, "document_id": document_id, "input_markdown": BODY}), Json({"summary": BODY})),
        )
        conn.execute(
            "INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, command, result, status) "
            "VALUES (%s, 'ws', 'user', 'query', %s, %s, 'completed')",
            (f"query-{document_id}", Json({"run_id": f"query-{document_id}"}), Json({"answer": "답변", "evidence_snippets": [
                {"source_document_id": document_id, "text": BODY}, {"source_document_id": other, "text": OTHER_TEXT}]})),
        )
        conn.execute(
            "INSERT INTO pipeline_runs(id, document_id, workspace_id, input_source, output_dir, mode, status, manifest) "
            "VALUES (%s, %s, 'ws', 'i', 'o', 'm', 'succeeded', %s)", (pipeline, document_id, Json({"x": BODY})),
        )
        conn.execute(
            "INSERT INTO document_quality_reviews(document_id, workspace_id, ingest_run_id, evaluations) VALUES (%s, 'ws', %s, %s)",
            (document_id, pipeline, Json([{"answer": BODY}])),
        )
        conn.execute("INSERT INTO source_blocks(document_id, block_id, text) VALUES (%s, 'b', %s)", (document_id, BODY))
        conn.execute(
            "INSERT INTO ai_task_changes(run_id, table_name, row_key, before_value, after_value) VALUES (%s, 'source_blocks', %s, NULL, %s)",
            (run, Json({"block_id": "b", "document_id": document_id}), Json({"text": BODY, "document_id": document_id})),
        )
    _source_page(connect, page_id, document_id)
    wiki_objects.keys |= {f"wiki/ws/sources/{document_id}.md", f"wiki/ws/pages/{page_id}/1.md"}


def _rows(connect, table: str, where: str, *params) -> list[dict]:
    with connect() as conn:
        return conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()


def _run(ids=frozenset({LIVE}), **values):
    return reconcile.reconcile(set(ids), NOW, **values)


def _apply(ids=frozenset({LIVE}), **values):
    return _run(ids, apply=True, expected_count=_run(ids)["targets"], **values)


def test_dry_run_counts_tables_without_ids_and_changes_nothing(stack, wiki_objects, tmp_path) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _tombstone(connect, GONE)
    _seed_copies(connect, wiki_objects, GONE, "gone-page")
    _seed_copies(connect, wiki_objects, LIVE, "live-page")
    ids_out = tmp_path / "ids.tsv"

    result = _run(ids_out=str(ids_out))

    assert result["dry_run"] is True and result["targets"] == 1 and result["workspaces"] == 1
    # 다른 문서 run의 근거도 함께 센다. 다른 문서 행은 세지 않는다.
    assert result["estimated"]["ai_task_runs"] == 3  # 이 문서의 run, 이 문서의 query, 근거가 섞인 다른 문서의 query
    assert result["estimated"]["pipeline_runs"] == 1
    assert result["estimated"]["document_quality_reviews"] == 1
    assert result["estimated"]["wiki_pages"] == 1
    assert result["estimated"]["ai_task_changes"] == 1
    assert result["estimated"]["agent_runs"] == 0
    assert result["estimated"]["s3_source_objects"] == 2
    printed = json.dumps(result, ensure_ascii=False)
    assert GONE not in printed and "gone-page" not in printed
    assert stat.S_IMODE(os.stat(ids_out).st_mode) == 0o600
    assert ids_out.read_text(encoding="utf-8") == f"ws\t{GONE}\n"
    assert _rows(connect, "pipeline_runs", "document_id = %s", GONE)[0]["manifest"] == {"x": BODY}
    assert _rows(connect, "ai_document_purges", "true") == []
    assert f"wiki/ws/sources/{GONE}.md" in wiki_objects.keys


def test_ids_out_narrows_the_mode_of_an_existing_file(stack, tmp_path) -> None:  # noqa: F811
    _, _, connect, _ = stack
    ids_out = tmp_path / "ids.tsv"
    ids_out.write_text("x", encoding="utf-8")
    os.chmod(ids_out, 0o644)
    _tombstone(connect, GONE)

    _run(ids_out=str(ids_out))

    assert stat.S_IMODE(os.stat(ids_out).st_mode) == 0o600


def test_apply_removes_only_the_missing_documents_copies_and_rerun_changes_nothing(stack, wiki_objects) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _tombstone(connect, GONE)
    _seed_copies(connect, wiki_objects, GONE, "gone-page")
    _seed_copies(connect, wiki_objects, LIVE, "live-page")

    result = _apply()

    assert result["dry_run"] is False and result["outcomes"] == {"finished": 1} and result["source_pages"] == 1
    assert _rows(connect, "pipeline_runs", "document_id = %s", GONE)[0]["manifest"] == {"scrubbed": True}
    assert _rows(connect, "document_quality_reviews", "document_id = %s", GONE) == []
    assert _rows(connect, "source_blocks", "document_id = %s", GONE) == []
    assert _rows(connect, "ai_task_changes", "row_key->>'document_id' = %s", GONE) == []
    assert _rows(connect, "wiki_pages", "id = 'gone-page'")[0]["summary"] == ""
    assert not {f"wiki/ws/sources/{GONE}.md", "wiki/ws/pages/gone-page/1.md"} & wiki_objects.keys
    # 살아 있는 문서는 그대로다. 같은 run에 섞인 근거도 없는 문서의 것만 비운다.
    assert _rows(connect, "pipeline_runs", "document_id = %s", LIVE)[0]["manifest"] == {"x": BODY}
    assert _rows(connect, "source_blocks", "document_id = %s", LIVE)[0]["text"] == BODY
    assert _rows(connect, "ai_task_changes", "row_key->>'document_id' = %s", LIVE)
    assert _rows(connect, "wiki_pages", "id = 'live-page'")[0]["summary"] == BODY
    assert {f"wiki/ws/sources/{LIVE}.md", "wiki/ws/pages/live-page/1.md"} <= wiki_objects.keys
    evidence = _rows(connect, "ai_task_runs", "id = %s", f"query-{GONE}")[0]["result"]["evidence_snippets"]
    assert [item["text"] for item in evidence] == ["", OTHER_TEXT]
    live_evidence = _rows(connect, "ai_task_runs", "id = %s", f"query-{LIVE}")[0]["result"]["evidence_snippets"]
    assert [item["text"] for item in live_evidence] == [BODY, ""]

    again = _run()
    assert again["targets"] == 0 and again["skipped"]["already_finished"] == 1
    assert _apply()["outcomes"] == {}


@pytest.mark.parametrize(
    ("tombstone", "existing"),
    [
        ("chatdoc_abc", {"chatdoc_abc"}),      # 그대로 있다
        ("chatdoc-abc", {"chatdoc_abc"}),      # 정규화한 id로 표시됐다
        ("chatdoc_abc", {"chatdoc-abc"}),      # 목록이 정규화된 id다
        ("Doc_1", {"doc-1"}),                  # 슬러그가 같은 살아 있는 문서의 소스 페이지를 지우면 안 된다
    ],
)
def test_ids_matching_before_or_after_normalization_are_not_targets(stack, tombstone, existing) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _tombstone(connect, tombstone)

    result = _run(existing)

    assert result["targets"] == 0 and result["skipped"] == {"exists_in_core": 1}


def test_nonstandard_missing_id_reaches_its_normalized_source_page(stack, wiki_objects) -> None:  # noqa: F811
    _, _, connect, _ = stack
    document_id = "chatdoc_0a1B2c"
    _tombstone(connect, document_id)
    _source_page(connect, "chat-page", document_id)
    wiki_objects.keys |= {f"wiki/ws/sources/{document_id}.md", "wiki/ws/pages/chat-page/1.md"}

    result = _apply()

    assert result["outcomes"] == {"finished": 1}
    assert _rows(connect, "wiki_pages", "id = 'chat-page'")[0]["markdown_uri"] is None
    assert not {f"wiki/ws/sources/{document_id}.md", "wiki/ws/pages/chat-page/1.md"} & wiki_objects.keys


def test_trashed_document_in_the_list_recent_and_foreign_scopes_are_skipped(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _tombstone(connect, "in-trash")
    _tombstone(connect, "recent", NOW - timedelta(hours=1))
    _tombstone(connect, "purged-scope", workspace_id="ws-purged")
    _tombstone(connect, "moved")
    with connect() as conn:
        conn.execute("INSERT INTO ai_purged_scopes(scope_type, scope_id) VALUES ('workspace', 'ws-purged')")
        conn.execute("INSERT INTO pipeline_runs(id, document_id, workspace_id, input_source, output_dir, mode, status) "
                     "VALUES ('44444444-4444-4444-4444-444444444444', 'moved', 'other-ws', 'i', 'o', 'm', 'succeeded')")

    result = _run({"in-trash"})

    assert result["targets"] == 0
    assert result["skipped"] == {"exists_in_core": 1, "other_workspace": 1, "too_recent": 1, "workspace_purged": 1}


def test_unfinished_request_is_tried_again(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _tombstone(connect, GONE)
    with connect() as conn:
        conn.execute("INSERT INTO ai_document_purges(workspace_id, document_id) VALUES ('ws', %s)", (GONE,))

    assert _apply()["outcomes"] == {"finished": 1}


def test_missing_document_without_tombstone_is_only_counted(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    with connect() as conn:
        conn.execute("INSERT INTO pipeline_runs(id, document_id, workspace_id, input_source, output_dir, mode, status, manifest, created_at) "
                     "VALUES ('55555555-5555-5555-5555-555555555555', 'no-mark', 'ws', 'i', 'o', 'm', 'succeeded', %s, %s)",
                     (Json({"x": BODY}), OLD))

    result = _apply()

    assert result["targets"] == 0 and result["missing_without_tombstone_not_targeted"] == 1
    assert _rows(connect, "pipeline_runs", "document_id = 'no-mark'")[0]["manifest"] == {"x": BODY}


def test_busy_shared_object_lock_leaves_the_document_for_the_worker(stack, wiki_objects, monkeypatch) -> None:  # noqa: F811
    _, _, connect, _ = stack
    monkeypatch.setattr(purge, "_SHARED_OBJECT_LOCK_TIMEOUT", "100ms")
    _tombstone(connect, "doc-1")
    wiki_objects.keys = {ACTIVE}
    wiki_objects.texts = {ACTIVE: _object(ACTIVE_TEXT)}
    assert _run()["estimated"]["s3_shared_objects_to_rewrite"] == 1

    with concept_write_lock("ws", "another-ingest"):
        result = _apply()

    assert result["outcomes"] == {"pending_shared_objects": 1}
    assert _rows(connect, "ai_document_purges", "document_id = 'doc-1'")[0]["finished_at"] is None
    assert BODY in wiki_objects.texts[ACTIVE]["text"]

    assert purge.purge_pending_documents().finished == 1
    assert _rows(connect, "ai_document_purges", "document_id = 'doc-1'")[0]["finished_at"] is not None
    assert BODY not in wiki_objects.texts[ACTIVE]["text"]


def test_active_run_keeps_the_request_open_until_it_ends(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _tombstone(connect, GONE)
    with connect() as conn:
        conn.execute("INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, command, status) "
                     "VALUES ('reading', 'ws', 'user', 'query', %s, 'running')", (Json({"run_id": "reading"}),))

    assert _apply()["outcomes"] == {"waiting_active_runs": 1}
    assert _rows(connect, "ai_document_purges", "document_id = %s", GONE)[0]["finished_at"] is None

    with connect() as conn:
        conn.execute("UPDATE ai_task_runs SET status = 'completed' WHERE id = 'reading'")
    assert purge.purge_pending_documents().finished == 1


def test_storage_failure_leaves_the_document_for_the_worker_and_is_only_counted_in_dry_run(stack, wiki_objects) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _tombstone(connect, GONE)
    _source_page(connect, "gone-page", GONE)
    wiki_objects.keys |= {f"wiki/ws/sources/{GONE}.md"}
    wiki_objects.failing = True

    assert _run()["storage_estimate_errors"] == 1
    assert "pending_source_objects" in _apply()["outcomes"]

    wiki_objects.failing = False
    assert purge.purge_pending_documents().finished == 1
    assert not wiki_objects.keys


def test_short_list_is_refused_by_active_source_coverage(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _tombstone(connect, GONE)
    for index in range(4):
        _source_page(connect, f"page-{index}", f"live-{index}", status="active")

    with pytest.raises(SystemExit, match="잘린"):
        _run({"live-0"})

    assert _run({"live-0", "live-1", "live-2"})["targets"] == 1


def test_apply_refuses_a_count_different_from_the_dry_run(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _tombstone(connect, GONE)

    with pytest.raises(SystemExit, match="expected-count"):
        _run(apply=True, expected_count=2)
    with pytest.raises(SystemExit, match="expected-count"):
        _run(apply=True)

    assert _rows(connect, "ai_document_purges", "true") == []


@pytest.mark.parametrize("exported_at", [NOW + timedelta(hours=1), NOW - timedelta(days=2)])
def test_future_or_stale_export_is_refused(exported_at) -> None:
    with pytest.raises(SystemExit, match="exported-at"):
        reconcile.reconcile({LIVE}, exported_at, now=lambda: NOW)


def test_empty_or_short_file_is_refused(tmp_path) -> None:
    path = tmp_path / "ids.txt"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="빈 파일"):
        reconcile.read_document_ids(str(path), 1)
    path.write_text("a\nb\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="min-documents"):
        reconcile.read_document_ids(str(path), 3)
    assert reconcile.read_document_ids(str(path), 2) == {"a", "b"}


def test_exported_at_requires_timezone() -> None:
    assert reconcile.parse_exported_at("2026-11-07T12:00:00+09:00").utcoffset() is not None
    with pytest.raises(Exception):
        reconcile.parse_exported_at("2026-11-07T12:00:00")
