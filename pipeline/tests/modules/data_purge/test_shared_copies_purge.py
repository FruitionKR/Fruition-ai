"""실제 왕복에서 남은 복사본(공유 객체, 작업 기록, 소유 관계로만 찾는 행, 진행 중 run의 늦은 쓰기)을 검증한다."""

from uuid import uuid4

import pytest
from psycopg.types.json import Json

from app.modules.data_purge.infrastructure import document_copy_purge as purge
from app.modules.data_purge.infrastructure.purged_document_markdown import (
    without_document_clusters,
    without_document_log,
)
from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
from app.modules.task_cancellation.infrastructure.object_change_journal import undo_object
from app.modules.wiki_generation.infrastructure.meaning_cluster_artifact import _id_fragment
from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import concept_write_lock
from tests.modules.agent_run.test_agent_run_postgres import _workflow_run, repositories  # noqa: F401
from tests.modules.agent_run.test_expired_artifact_cleanup import cleanup_storage, storage  # noqa: F401
from tests.modules.data_purge.test_document_copy_purge import (  # noqa: F401
    BODY,
    RUN_ID,
    _purge,
    _task_run,
    stack,
    wiki_objects,
)

OTHER = "다른 문서 내용"
CONTENT_TYPE = "text/markdown; charset=utf-8"
ACTIVE = "wiki/ws/clusters/active.md"
ARCHIVED = "wiki/ws/clusters/archived.md"
LOG = "wiki/ws/logs/2026-11-04.md"


def _cluster(cluster_id: str, *claims: str, promotion: str = "") -> str:
    return "\n".join([f"## cluster: {cluster_id}", "", "type: term_cluster", f"representative: {cluster_id}", "",
                      "### Evidence Claims", *claims]) + promotion


def _active(*sections: str) -> str:
    return "\n\n".join(["# Active Meaning Clusters", *sections]) + "\n"


def _log_section(date: str, kind: str, subject: str, claim: str) -> str:
    return f"## {date} {kind}: {subject}\n\nuser: u\n\n### Extracted Claims\n- {claim}\n"


ACTIVE_TEXT = _active(
    _cluster("mixed", f"- claim_aaaa_001: {BODY} [doc-1:B0001]", f"- claim_bbbb_001: {OTHER} [doc-2:B0001]"),
    _cluster("only-purged", f"- claim_aaaa_002: {BODY} [doc-1:B0002]"),
    _cluster("other", f"- claim_bbbb_002: {OTHER} [doc-2:B0002]"),
)
LOG_TEXT = (_log_section("2026-11-04", "ingest", "doc-1", f"claim_aaaa_001: {BODY} [doc-1:B0001]")
            + _log_section("2026-11-04", "ingest", "doc-2", f"claim_bbbb_001: {OTHER} [doc-2:B0001]")
            + _log_section("2026-11-04", "lint", "ws", "cluster:mixed"))


def _object(text: str | None) -> dict[str, str] | None:
    return None if text is None else {"text": text, "content_type": CONTENT_TYPE}


def _journal_object(connect, run_id: str, key: str, before: str | None, after: str | None) -> int:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO ai_task_changes(run_id, table_name, row_key, before_value, after_value) "
            "VALUES (%s, '__object__', %s, %s, %s) RETURNING id",
            (run_id, Json({"bucket": "b", "key": key}), Json(_object(before)), Json(_object(after))),
        ).fetchone()["id"]


def _change(connect, change_id: int) -> dict:
    with connect() as conn:
        return conn.execute("SELECT * FROM ai_task_changes WHERE id = %s", (change_id,)).fetchone()


def _pipeline_run(connect, *, document_id: str | None, manifest: dict, run_id: str | None = None) -> str:
    run_id = run_id or str(uuid4())
    with connect() as conn:
        conn.execute("INSERT INTO pipeline_runs(id, document_id, workspace_id, input_source, output_dir, mode, status, manifest) "
                     "VALUES (%s, %s, 'ws', 'i', 'o', 'post_ingest', 'succeeded', %s)",
                     (run_id, document_id, Json(manifest)))
    return run_id


def _post_ingest(connect, document_id: str, manifest: dict) -> str:
    """실측과 같게 pipeline_runs.document_id는 NULL이고, 어느 문서인지는 같은 id의 작업 command에만 있다."""
    run_id = _pipeline_run(connect, document_id=None, manifest=manifest)
    _task_run(connect, run_id, "post_ingest", {"run_id": run_id, "document_id": document_id})
    return run_id


# --- 공유 객체의 텍스트 ---


def test_clusters_lose_only_the_purged_documents_claims() -> None:
    stripped = without_document_clusters(ACTIVE_TEXT, "doc-1")

    assert BODY not in stripped
    assert "only-purged" not in stripped  # 근거가 없어진 클러스터는 대표명과 함께 지운다.
    assert f"- claim_bbbb_001: {OTHER} [doc-2:B0001]" in stripped
    assert _cluster("other", f"- claim_bbbb_002: {OTHER} [doc-2:B0002]") in stripped
    assert without_document_clusters(stripped, "doc-1") == stripped  # 재전달해도 같다.


def test_clusters_are_untouched_when_the_document_is_not_cited() -> None:
    assert without_document_clusters(ACTIVE_TEXT, "doc-9") == ACTIVE_TEXT


def test_clusters_do_not_match_another_document_whose_id_ends_with_the_same_text() -> None:
    text = _active(_cluster("near", f"- claim_cccc_001: {OTHER} [xdoc-1:B0001]"))

    assert without_document_clusters(text, "doc-1") == text


def test_multi_line_claim_is_removed_as_a_whole() -> None:
    text = _active(_cluster(
        "mixed",
        f"- claim_aaaa_001: {BODY} 첫 줄\n    {BODY} 둘째 줄 [doc-1:B0001]",
        f"- claim_bbbb_001: {OTHER} [doc-2:B0001]",
    ))

    stripped = without_document_clusters(text, "doc-1")

    assert BODY not in stripped and f"{OTHER} [doc-2:B0001]" in stripped


def test_claim_without_a_citation_is_recognized_by_the_documents_claim_id() -> None:
    claim_id = f"claim_{_id_fragment('doc-1')}_001"
    text = _active(_cluster("mixed", f"- {claim_id}: {BODY}", f"- claim_bbbb_001: {OTHER} [doc-2:B0001]"))

    assert BODY not in without_document_clusters(text, "doc-1")


def test_promotion_keeps_surviving_sources_and_empty_cluster_goes_with_its_reason() -> None:
    promotion = "\n\n### Promotion\nstatus: candidate\nsource_refs: [doc-1, doc-2]\nreason: 근거\n"
    mixed = _active(_cluster("mixed", f"- claim_aaaa_001: {BODY} [doc-1:B0001]",
                             f"- claim_bbbb_001: {OTHER} [doc-2:B0001]", promotion=promotion))
    only = _active(_cluster("only", f"- claim_aaaa_001: {BODY} [doc-1:B0001]", promotion=promotion))

    assert "source_refs: [doc-2]" in without_document_clusters(mixed, "doc-1")
    assert "reason: 근거" not in without_document_clusters(only, "doc-1")


def test_log_loses_only_the_purged_documents_ingest_sections() -> None:
    stripped = without_document_log(LOG_TEXT, "doc-1")

    assert BODY not in stripped and "ingest: doc-1" not in stripped
    assert _log_section("2026-11-04", "ingest", "doc-2", f"claim_bbbb_001: {OTHER} [doc-2:B0001]") in stripped
    assert "lint: ws" in stripped
    assert without_document_log(stripped, "doc-1") == stripped


# --- 객체와 그 작업 기록 ---


def test_shared_objects_and_their_journal_are_rewritten_for_every_run_of_the_workspace(stack, wiki_objects) -> None:  # noqa: F811
    _, _, connect, _ = stack
    wiki_objects.texts = {ACTIVE: _object(ACTIVE_TEXT), LOG: _object(LOG_TEXT),
                          "wiki/other/clusters/active.md": _object(ACTIVE_TEXT)}
    wiki_objects.keys = set(wiki_objects.texts)
    _task_run(connect, "own", "post_ingest", {"run_id": "own", "document_id": "doc-1"})
    _task_run(connect, "later", "post_ingest", {"run_id": "later", "document_id": "doc-2"})
    own = _journal_object(connect, "own", ACTIVE, None, ACTIVE_TEXT)
    later = _journal_object(connect, "later", LOG, LOG_TEXT, LOG_TEXT + _log_section("2026-11-04", "lint", "ws", "x"))
    with connect() as conn:
        conn.execute("INSERT INTO ai_task_runs(id, workspace_id, user_id, kind) VALUES ('foreign', 'other', 'u', 'lint')")
    foreign = _journal_object(connect, "foreign", "wiki/other/clusters/active.md", None, ACTIVE_TEXT)

    assert _purge().finished

    assert BODY not in wiki_objects.texts[ACTIVE]["text"] and OTHER in wiki_objects.texts[ACTIVE]["text"]
    assert BODY not in wiki_objects.texts[LOG]["text"] and OTHER in wiki_objects.texts[LOG]["text"]
    assert wiki_objects.texts[ACTIVE]["content_type"] == CONTENT_TYPE
    assert wiki_objects.texts["wiki/other/clusters/active.md"]["text"] == ACTIVE_TEXT  # 다른 워크스페이스
    assert BODY not in str(_change(connect, own)["after_value"]) and OTHER in str(_change(connect, own)["after_value"])
    assert BODY not in str(_change(connect, later)["before_value"]) and OTHER in str(_change(connect, later)["before_value"])
    assert BODY not in str(_change(connect, later)["after_value"])
    assert BODY in str(_change(connect, foreign)["after_value"])


def test_archived_clusters_are_rewritten_too(stack, wiki_objects) -> None:  # noqa: F811
    wiki_objects.texts = {ARCHIVED: _object(_cluster("gone", f"- claim_aaaa_001: {BODY} [doc-1:B0001]") + "\n\n"
                                            + _cluster("kept", f"- claim_bbbb_001: {OTHER} [doc-2:B0001]") + "\n")}
    wiki_objects.keys = set(wiki_objects.texts)

    assert _purge().finished

    assert BODY not in wiki_objects.texts[ARCHIVED]["text"] and OTHER in wiki_objects.texts[ARCHIVED]["text"]


def test_rollback_still_matches_rewritten_journal_and_restores_no_body(stack, wiki_objects) -> None:  # noqa: F811
    _, _, connect, _ = stack
    previous = _active(_cluster("mixed", f"- claim_aaaa_001: {BODY} [doc-1:B0001]"),
                       _cluster("other", f"- claim_bbbb_002: {OTHER} [doc-2:B0002]"))
    wiki_objects.texts = {ACTIVE: _object(ACTIVE_TEXT)}
    wiki_objects.keys = {ACTIVE}
    _task_run(connect, "later", "post_ingest", {"run_id": "later", "document_id": "doc-2"})
    change = _journal_object(connect, "later", ACTIVE, previous, ACTIVE_TEXT)

    assert _purge().finished
    undo_object(_change(connect, change))

    assert BODY not in wiki_objects.texts[ACTIVE]["text"] and OTHER in wiki_objects.texts[ACTIVE]["text"]
    assert _change(connect, change)["undone"] is True


def test_busy_workspace_lock_leaves_the_object_for_the_next_cycle_without_blocking(stack, wiki_objects, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(purge, "_SHARED_OBJECT_LOCK_TIMEOUT", "100ms")
    wiki_objects.texts = {ACTIVE: _object(ACTIVE_TEXT)}
    wiki_objects.keys = {ACTIVE}

    with concept_write_lock("ws", "another-ingest"):
        # 같은 스레드의 잠금 재사용은 run id가 달라 일어나지 않는다. 잠금은 PostgreSQL이 판정한다.
        result = _purge()

    assert result.pending_shared_objects == 1 and not result.finished
    assert BODY in wiki_objects.texts[ACTIVE]["text"]
    assert purge.purge_pending_documents().finished == 1
    assert BODY not in wiki_objects.texts[ACTIVE]["text"]


def test_storage_failure_on_shared_objects_is_retried(stack, wiki_objects) -> None:  # noqa: F811
    wiki_objects.texts = {ACTIVE: _object(ACTIVE_TEXT)}
    wiki_objects.keys = {ACTIVE}
    wiki_objects.failing = True

    assert not _purge().finished  # 예외를 올리지 않아 파티션을 막지 않는다.

    wiki_objects.failing = False
    assert purge.purge_pending_documents().finished == 1
    assert BODY not in wiki_objects.texts[ACTIVE]["text"]


# --- 소유 관계로 찾는 행 ---


def test_post_ingest_manifest_is_found_through_its_task_run(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    manifest = {"cluster_artifact": {"active_markdown": BODY, "clusters": [{"evidence_claims": [{"claim": BODY}]}]}}
    own = _post_ingest(connect, "doc-1", manifest)
    other = _post_ingest(connect, "doc-2", {"cluster_artifact": {"active_markdown": OTHER}})

    assert _purge().finished

    with connect() as conn:
        rows = {row["id"]: row["manifest"] for row in conn.execute("SELECT id::text AS id, manifest FROM pipeline_runs")}
    assert rows[own] == {"scrubbed": True}
    assert rows[other] == {"cluster_artifact": {"active_markdown": OTHER}}


def test_other_workspace_run_with_the_same_document_id_is_not_touched(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    run_id = str(uuid4())
    with connect() as conn:
        conn.execute("INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, command, status) "
                     "VALUES (%s, 'other', 'u', 'post_ingest', %s, 'completed')",
                     (run_id, Json({"document_id": "doc-1"})))
        conn.execute("INSERT INTO pipeline_runs(id, document_id, workspace_id, input_source, output_dir, mode, status, manifest) "
                     "VALUES (%s, NULL, 'other', 'i', 'o', 'post_ingest', 'succeeded', %s)", (run_id, Json({"claim": BODY})))

    assert _purge().finished

    with connect() as conn:
        assert conn.execute("SELECT manifest FROM pipeline_runs").fetchone()["manifest"] == {"claim": BODY}


def test_post_ingest_journal_rows_are_deleted_through_their_run(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    own = _post_ingest(connect, "doc-1", {})
    other = _post_ingest(connect, "doc-2", {})
    with connect() as conn:
        for run_id in (own, other):
            conn.execute("INSERT INTO ai_task_changes(run_id, table_name, row_key, before_value, after_value) "
                         "VALUES (%s, 'pipeline_runs', %s, %s, %s)",
                         (run_id, Json({"id": run_id}), Json({"id": run_id, "document_id": None, "manifest": {}}),
                          Json({"id": run_id, "document_id": None, "manifest": {"claim": BODY if run_id == own else OTHER}})))

    assert _purge().finished

    with connect() as conn:
        assert [row["run_id"] for row in conn.execute("SELECT run_id FROM ai_task_changes")] == [other]


def test_source_object_journal_row_is_deleted_and_others_stay(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _task_run(connect, "own", "document", {"run_id": "own", "document_id": "doc-1"})
    _task_run(connect, "other", "document", {"run_id": "other", "document_id": "doc-2"})
    own = _journal_object(connect, "own", "wiki/ws/sources/doc-1.md", None, BODY)
    other = _journal_object(connect, "other", "wiki/ws/sources/doc-2.md", None, OTHER)

    assert _purge().finished

    with connect() as conn:
        assert [row["id"] for row in conn.execute("SELECT id FROM ai_task_changes WHERE id = ANY(%s)", ([own, other],))] == [other]


def test_evidence_in_other_runs_journal_is_blanked_in_both_columns(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    evidence = {"manifest": {"quality_cases": [
        {"source_document_id": "doc-1", "expected_claims": [BODY]},
        {"source_document_id": "doc-2", "expected_claims": [OTHER]},
    ]}}
    run = _post_ingest(connect, "doc-2", {})
    with connect() as conn:
        change = conn.execute(
            "INSERT INTO ai_task_changes(run_id, table_name, row_key, before_value, after_value) "
            "VALUES (%s, 'pipeline_runs', %s, %s, %s) RETURNING id",
            (run, Json({"id": run}), Json(evidence), Json(evidence)),
        ).fetchone()["id"]

    assert _purge().finished

    row = _change(connect, change)
    for value in (row["before_value"], row["after_value"]):
        assert BODY not in str(value) and OTHER in str(value)


def test_cancelling_another_documents_run_still_matches_its_blanked_row(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    manifest = {"quality_cases": [{"source_document_id": "doc-1", "expected_claims": [BODY]},
                                  {"source_document_id": "doc-2", "expected_claims": [OTHER]}]}
    run = _post_ingest(connect, "doc-2", {})
    with connect() as conn:
        conn.execute("UPDATE pipeline_runs SET manifest = %s WHERE id = %s", (Json(manifest), run))
        row = conn.execute("SELECT to_jsonb(p) AS value FROM pipeline_runs p WHERE id = %s", (run,)).fetchone()["value"]
        before = {**row, "manifest": {"previous": True, **manifest}}
        change = conn.execute(
            "INSERT INTO ai_task_changes(run_id, table_name, row_key, before_value, after_value) "
            "VALUES (%s, 'pipeline_runs', %s, %s, %s) RETURNING id",
            (run, Json({"id": run}), Json(before), Json(row)),
        ).fetchone()["id"]

    assert _purge().finished
    journal.undo_row(_change(connect, change))  # 현재 값과 변경 후 값이 같아야 충돌하지 않는다.

    with connect() as conn:
        restored = conn.execute("SELECT manifest FROM pipeline_runs WHERE id = %s", (run,)).fetchone()["manifest"]
    assert restored["previous"] is True and BODY not in str(restored) and OTHER in str(restored)


# --- 진행 중 run의 늦은 쓰기 ---


def _running(connect, run_id: str = "query-live", *, workspace: str = "ws", age: str = "0 seconds") -> None:
    with connect() as conn:
        conn.execute("INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, command, status, created_at) "
                     "VALUES (%s, %s, 'u', 'query', %s, 'running', now() - %s::interval)",
                     (run_id, workspace, Json({"run_id": run_id}), age))


def _evidence(document_id: str = "doc-1") -> dict:
    return {"evidence_snippets": [{"source_document_id": document_id, "text": BODY}]}


def _rewind_last_attempt(connect) -> None:
    with connect() as conn:
        conn.execute("UPDATE ai_document_purges SET last_attempt_at = last_attempt_at - interval '1 hour'")


def test_result_written_after_the_scan_is_blanked_once_the_run_ends(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _running(connect)

    result = _purge()

    assert result.active_runs == 1 and not result.finished
    journal.complete("query-live", _evidence())  # 스캔 전에 근거를 읽은 질의가 스캔 뒤에 결과를 쓴다.
    assert purge.purge_pending_documents().finished == 1
    with connect() as conn:
        stored = conn.execute("SELECT result FROM ai_task_runs WHERE id = 'query-live'").fetchone()["result"]
    assert stored == {"evidence_snippets": [{"source_document_id": "doc-1", "text": ""}]}


def test_checkpoint_written_after_the_scan_is_scrubbed_once_the_run_ends(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    run_id = str(uuid4())
    _pipeline_run(connect, document_id=None, manifest={}, run_id=run_id)
    _task_run(connect, run_id, "post_ingest", {"run_id": run_id, "document_id": "doc-1"}, status="running")

    assert not _purge().finished
    with connect() as conn:
        conn.execute("UPDATE pipeline_runs SET manifest = %s WHERE id = %s", (Json({"cluster_artifact": {"claim": BODY}}), run_id))
        conn.execute("UPDATE ai_task_runs SET status = 'completed' WHERE id = %s", (run_id,))
    assert purge.purge_pending_documents().finished == 1

    with connect() as conn:
        assert conn.execute("SELECT manifest FROM pipeline_runs").fetchone()["manifest"] == {"scrubbed": True}


def test_running_agent_run_also_delays_finishing(stack) -> None:  # noqa: F811
    runs, _, connect, _ = stack
    runs.create_with_planning_job(_workflow_run(), "edit-job")
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status = 'executing' WHERE id = %s", (RUN_ID,))

    assert _purge().active_runs == 1

    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status = 'completed' WHERE id = %s", (RUN_ID,))
    assert purge.purge_pending_documents().finished == 1


def test_awaiting_approval_agent_run_does_not_delay_finishing(stack) -> None:  # noqa: F811
    runs, _, connect, _ = stack
    runs.create_with_planning_job(_workflow_run(), "edit-job")
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status = 'awaiting_approval' WHERE id = %s", (RUN_ID,))

    assert _purge().finished


def test_run_started_after_the_last_scan_cannot_have_read_the_body(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _running(connect, "old-run")
    _purge()
    with connect() as conn:
        conn.execute("UPDATE ai_task_runs SET status = 'completed' WHERE id = 'old-run'")
    _rewind_last_attempt(connect)  # 지난 스캔이 끝난 시각을 새 run보다 앞으로 둔다.
    _running(connect, "new-run")

    assert purge.purge_pending_documents().finished == 1


def test_other_workspace_and_abandoned_runs_do_not_delay_finishing(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _running(connect, "foreign", workspace="other")
    _running(connect, "stuck", age="2 days")

    assert _purge().finished


def test_waiting_for_runs_is_not_counted_as_a_storage_failure(stack) -> None:  # noqa: F811
    _, _, connect, _ = stack
    _running(connect)
    for index in range(purge.MAX_STORAGE_FAILURES_PER_CYCLE + 2):
        _purge(f"waiting-{index}")
    with connect() as conn:
        conn.execute("UPDATE ai_document_purges SET last_attempt_at = NULL")

    result = purge.purge_pending_documents()

    assert result.pending == purge.MAX_STORAGE_FAILURES_PER_CYCLE + 2  # 멈추지 않고 전부 다시 훑는다.


def test_redelivery_after_finishing_changes_nothing(stack, wiki_objects) -> None:  # noqa: F811
    wiki_objects.texts = {ACTIVE: _object(ACTIVE_TEXT), LOG: _object(LOG_TEXT)}
    wiki_objects.keys = set(wiki_objects.texts)

    assert _purge().finished
    first = dict(wiki_objects.texts)
    assert _purge().finished

    assert wiki_objects.texts == first


@pytest.mark.parametrize("key", [ACTIVE, LOG])
def test_object_not_citing_the_document_is_not_rewritten(stack, wiki_objects, monkeypatch, key) -> None:  # noqa: F811
    text = _active(_cluster("other", f"- claim_bbbb_002: {OTHER} [doc-2:B0002]"))
    wiki_objects.texts = {key: _object(text)}
    wiki_objects.keys = {key}
    writes: list[str] = []
    put_object = wiki_objects.put_object
    monkeypatch.setattr(wiki_objects, "put_object", lambda bucket, name, *a, **k: (writes.append(name), put_object(bucket, name, *a, **k)))

    assert _purge().finished

    assert writes == []
