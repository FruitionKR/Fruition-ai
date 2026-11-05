"""문서 영구 삭제(document_purged)가 AI 쪽 본문 복사본을 지우는지 실제 PostgreSQL로 검증한다. 객체 저장소만 가짜다."""

import hashlib
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from minio.error import S3Error
from psycopg.types.json import Json

from app.modules.agent_run.application.approve_agent_plan import ApproveAgentPlanUseCase
from app.modules.agent_run.application.rollback_agent_run import RollbackAgentRunUseCase
from app.modules.agent_run.application.ports import ToolGatewayError
from app.modules.agent_run.domain.entities import AgentRun
from app.modules.agent_run.domain.plan import AgentPlanOperation, build_agent_plan
from app.modules.agent_run.infrastructure.postgres_agent_run_repository import (
    _artifact_object_key,
    _preview_object_key,
)
from app.modules.data_purge.infrastructure import document_copy_purge as purge
from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
from app.modules.wiki_generation.domain.text_utils import slugify
from tests.modules.agent_run.test_agent_run_postgres import (  # noqa: F401
    _edit_artifact,
    _plan_for,
    _workflow_run,
    repositories,
)
from tests.modules.agent_run.test_expired_artifact_cleanup import (  # noqa: F401
    cleanup_storage,
    storage,
)

RUN_ID = "edit-run"
BODY = "비밀 본문"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class WikiObjects:
    """위키 객체 저장소. 소스 페이지 객체 삭제를 기록하고 실패를 끼워 넣는다."""

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.texts: dict[str, dict[str, str]] = {}  # 본문을 읽고 다시 쓰는 공유 객체
        self.failing = False

    def get_object(self, bucket, key):
        if self.failing:
            raise OSError("storage down")
        if key not in self.texts:
            raise S3Error(code="NoSuchKey", message="missing", resource="", request_id="", host_id="", response=None)
        value = self.texts[key]
        return SimpleNamespace(read=lambda: value["text"].encode(), headers={"Content-Type": value["content_type"]},
                               close=lambda: None, release_conn=lambda: None)

    def put_object(self, bucket, key, data, length, content_type):
        if self.failing:
            raise OSError("storage down")
        self.texts[key] = {"text": data.read().decode(), "content_type": content_type}
        self.keys.add(key)

    def list_objects(self, bucket, prefix, recursive):
        assert recursive
        if self.failing:
            raise OSError("storage down")
        return [SimpleNamespace(object_name=key) for key in sorted(self.keys) if key.startswith(prefix)]

    def remove_object(self, bucket, key):
        if self.failing:
            raise OSError("storage down")
        self.keys.discard(key)
        self.texts.pop(key, None)


@pytest.fixture
def wiki_objects(monkeypatch) -> WikiObjects:
    fake = WikiObjects()
    monkeypatch.setattr(purge.object_storage, "client", lambda: fake)
    return fake


@pytest.fixture
def stack(repositories, cleanup_storage, wiki_objects, monkeypatch):  # noqa: F811
    runs, jobs, connect = repositories
    monkeypatch.setattr(journal, "connect", connect)
    monkeypatch.setenv("AI_DATABASE_URL", os.environ["TEST_AGENT_DATABASE_URL"])  # 워크스페이스 개념 잠금
    return runs, jobs, connect, cleanup_storage


def _purge(document_id: str = "doc-1", workspace_id: str = "ws"):
    return purge.purge_document(workspace_id, document_id)


def _awaiting(runs, *indexes: int):
    created = tuple(_edit_artifact(index) for index in indexes)
    runs.create_with_planning_job(_workflow_run(), "edit-job", created)
    _plan_for(runs, created)
    return created


def _keys(index: int) -> set[str]:
    artifact_id = _edit_artifact(index).id
    return {_artifact_object_key(RUN_ID, artifact_id), _preview_object_key(RUN_ID, artifact_id)}


def _execute_edit(runs, jobs, connect, index: int, operation_id: str) -> None:
    """실행 경로가 남기는 기록과 같게 스냅샷·전방 실행·되돌리기용 artifact를 만든다."""
    document_id = f"doc-{index}"
    jobs.prepare_undo(
        RUN_ID, "edit-plan", operation_id, "apply_document_edit", {"document_id": document_id},
        {"id": document_id, "markdown": f"{BODY} {index}", "content_hash": _sha(f"{BODY} {index}"), "edit_revision": 2},
    )
    jobs.save_tool_execution(
        run_id=RUN_ID, plan_id="edit-plan", operation_id=operation_id, tool_name="apply_document_edit",
        idempotency_key=f"agent:{RUN_ID}:edit-plan:{operation_id}", attempt=1, status="succeeded",
        response_metadata={"document_id": document_id, "current_version": 3, "content_hash": "h", "markdown": BODY},
        error_code=None,
    )
    with connect() as conn:
        conn.execute("UPDATE agent_plan_operations SET status = 'succeeded' WHERE id = %s", (operation_id,))


def _execute_two_documents(runs, jobs, connect) -> None:
    _awaiting(runs, 1, 2)
    run, plan = runs.get_current_plan_for_user("ws", "user", RUN_ID)
    runs.approve_and_enqueue(run, plan, "approval-1", "execution-job")
    _execute_edit(runs, jobs, connect, 1, "op-1")
    _execute_edit(runs, jobs, connect, 2, "op-2")


def _undo_metadata(connect, operation_id: str) -> dict:
    with connect() as conn:
        return conn.execute("SELECT response_metadata FROM agent_tool_executions WHERE idempotency_key = %s",
                            (f"undo:{operation_id}",)).fetchone()["response_metadata"]


# ── 단일 문서·여러 문서 ─────────────────────────────────────────────


def _finish_run(connect) -> None:
    """실행 중인 run은 끝난 뒤에 쓸 수 있어 삭제 정리가 끝나지 않는다."""
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status = 'completed' WHERE id = %s", (RUN_ID,))


def test_single_document_removes_artifact_objects_rows_and_snapshot(stack) -> None:
    runs, jobs, connect, objects = stack
    _execute_two_documents(runs, jobs, connect)
    _finish_run(connect)

    result = _purge()

    assert result.pending_artifacts == 0 and result.deleted_artifacts == 1
    assert _keys(1).isdisjoint(objects.objects)
    assert "before" not in _undo_metadata(connect, "op-1")
    assert _undo_metadata(connect, "op-1")["document_purged"] is True
    with connect() as conn:
        forward = conn.execute(
            "SELECT response_metadata FROM agent_tool_executions WHERE idempotency_key = %s",
            (f"agent:{RUN_ID}:edit-plan:op-1",),
        ).fetchone()["response_metadata"]
        document_ids = [row["document_id"] for row in conn.execute("SELECT document_id FROM agent_run_artifacts")]
        run = conn.execute("SELECT id FROM agent_runs WHERE id = %s", (RUN_ID,)).fetchone()
        operation = conn.execute("SELECT id, reason FROM agent_plan_operations WHERE id = 'op-1'").fetchone()
        request = conn.execute("SELECT finished_at FROM ai_document_purges WHERE document_id = 'doc-1'").fetchone()
    assert "markdown" not in forward and forward["document_id"] == "doc-1"
    assert document_ids == ["doc-2"]
    # run과 operation 행은 이력으로 남긴다.
    assert run is not None and operation is not None and "요약 1" not in operation["reason"]
    assert request["finished_at"] is not None


def test_multi_document_run_keeps_other_document_snapshot_and_objects(stack) -> None:
    runs, jobs, connect, objects = stack
    _execute_two_documents(runs, jobs, connect)

    _purge()

    assert _keys(2) <= set(objects.objects)
    other = _undo_metadata(connect, "op-2")
    assert other["before"]["markdown"] == f"{BODY} 2" and "document_purged" not in other
    with connect() as conn:
        reason = conn.execute("SELECT reason FROM agent_plan_operations WHERE id = 'op-2'").fetchone()["reason"]
    assert reason == "요약 2"


def test_other_workspace_with_same_document_id_is_untouched(stack) -> None:
    runs, jobs, connect, objects = stack
    _execute_two_documents(runs, jobs, connect)

    _purge(workspace_id="other-ws")

    assert _keys(1) <= set(objects.objects)
    assert _undo_metadata(connect, "op-1")["before"]["markdown"] == f"{BODY} 1"


def test_redelivery_is_idempotent(stack) -> None:
    runs, jobs, connect, objects = stack
    _execute_two_documents(runs, jobs, connect)

    first = _purge()
    before = _undo_metadata(connect, "op-1")
    second = _purge()

    assert first.deleted_artifacts == 1 and second.deleted_artifacts == 0
    assert _undo_metadata(connect, "op-1") == before
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS c FROM ai_document_purges").fetchone()["c"] == 1


def test_undo_and_created_document_artifacts_are_removed_by_link(stack) -> None:
    runs, jobs, connect, objects = stack
    runs.create_with_planning_job(_workflow_run(), "edit-job")
    artifact = runs.register_artifact(
        run_id=RUN_ID, workspace_id="ws", user_id="user", artifact_id="create-1",
        content_hash="sha256:" + _sha(BODY), purpose="create_document", document_id=None,
        base_version=None, target=None, markdown=BODY,
    )
    undo = runs.register_artifact(
        run_id=RUN_ID, workspace_id="ws", user_id="user", artifact_id="undo-x-1",
        content_hash="sha256:" + _sha(BODY), purpose="apply_document_edit", document_id="created-doc",
        base_version=1, target={"type": "whole_document", "start_line": 1, "end_line": 1}, markdown=BODY,
    )
    operation = AgentPlanOperation(
        id="op-create", sequence=1, tool_name="create_document", target_type="document", target_id=None,
        base_version=None, source_parent_id=None, destination_parent_id=None,
        arguments={"display_name": "새 문서", "folder_id": None, "content_artifact_id": artifact["id"],
                   "content_hash": artifact["content_hash"]},
        reason="새 문서 작성",
    )
    runs.save_plan(RUN_ID, build_agent_plan("edit-plan", RUN_ID, 1, "요약", (operation,)))
    jobs.save_tool_execution(
        run_id=RUN_ID, plan_id="edit-plan", operation_id="op-create", tool_name="create_document",
        idempotency_key=f"agent:{RUN_ID}:edit-plan:op-create", attempt=1, status="succeeded",
        response_metadata={"id": "created-doc", "current_version": 1}, error_code=None,
    )

    _purge("created-doc")

    assert undo["id"] == "undo-x-1"
    assert objects.objects == {}
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS c FROM agent_run_artifacts").fetchone()["c"] == 0


# ── 객체 실패·재시도 ────────────────────────────────────────────────


def test_object_failure_keeps_row_and_sweep_retries(stack) -> None:
    runs, jobs, connect, objects = stack
    _execute_two_documents(runs, jobs, connect)
    _finish_run(connect)
    failing = next(iter(_keys(1)))
    objects.failing.add(failing)

    result = _purge()

    assert result.pending_artifacts == 1
    # 본문이 담긴 DB 값은 객체 실패와 상관없이 먼저 지운다.
    assert "before" not in _undo_metadata(connect, "op-1")
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS c FROM agent_run_artifacts WHERE document_id = 'doc-1'"
        ).fetchone()["c"] == 1
        assert conn.execute("SELECT finished_at FROM ai_document_purges").fetchone()["finished_at"] is None

    assert purge.purge_pending_documents().finished == 0  # 아직 실패한다.

    objects.failing.clear()
    swept = purge.purge_pending_documents()

    assert swept.finished == 1
    assert _keys(1).isdisjoint(objects.objects) and _keys(2) <= set(objects.objects)
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS c FROM agent_run_artifacts WHERE document_id = 'doc-1'"
        ).fetchone()["c"] == 0
        assert conn.execute("SELECT finished_at FROM ai_document_purges").fetchone()["finished_at"] is not None


def test_key_outside_run_scope_is_not_deleted_and_stays_pending(stack, caplog) -> None:
    runs, jobs, connect, objects = stack
    _awaiting(runs, 1)
    with connect() as conn:
        conn.execute("UPDATE agent_run_artifacts SET object_key = 'wiki/ws/pages/p/ops/o.md'")

    result = _purge()

    assert result.pending_artifacts == 1
    assert "wiki/ws/pages/p/ops/o.md" not in objects.deleted


def test_purge_logs_never_contain_body(stack, caplog) -> None:
    runs, jobs, connect, objects = stack
    _execute_two_documents(runs, jobs, connect)
    objects.failing.add(next(iter(_keys(1))))

    with caplog.at_level("DEBUG"):
        _purge()
        objects.failing.clear()
        purge.purge_pending_documents()

    assert BODY not in caplog.text


def test_unexpected_failure_inside_purge_does_not_raise_to_the_consumer(stack, monkeypatch) -> None:
    monkeypatch.setattr(purge, "_scrub_rows", MagicMock(side_effect=RuntimeError("boom")))

    result = _purge()

    assert result.failed is True
    _, _, connect, _ = stack
    with connect() as conn:
        assert conn.execute("SELECT finished_at FROM ai_document_purges").fetchone()["finished_at"] is None


# ── 저널·실행 기록·위키 쪽 사본 ────────────────────────────────────


def _task_run(connect, run_id: str, kind: str, command: dict, result: dict | None = None, status: str = "completed") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO ai_task_runs(id, workspace_id, user_id, kind, command, result, status) "
            "VALUES (%s, 'ws', 'user', %s, %s, %s, %s)",
            (run_id, kind, Json(command), Json(result) if result is not None else None, status),
        )


def test_task_journal_body_is_removed_and_other_documents_stay(stack) -> None:
    _, _, connect, _ = stack
    _task_run(connect, "chat-1", "chat_wiki",
              {"run_id": "chat-1", "document_id": "doc-1", "input_markdown": BODY, "input_blocks": [{"text": BODY}]},
              {"summary": BODY})
    _task_run(connect, "chat-2", "chat_wiki",
              {"run_id": "chat-2", "document_id": "doc-2", "input_markdown": "다른 문서"}, {"summary": "다른 문서"})
    source_page = "source-page-1"
    with connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id, page_type, title, slug, user_id, workspace_id, status, created_at, updated_at) "
            "VALUES (%s, 'source', '제목', %s, 'user', 'ws', 'deleted', now(), now())",
            (source_page, slugify("doc-1")),
        )
        for table, key, before, after in [
            ("source_blocks", {"block_id": "b1", "document_id": "doc-1"}, None, {"text": BODY, "document_id": "doc-1"}),
            ("source_blocks", {"block_id": "b2", "document_id": "doc-2"}, None, {"text": "다른 문서", "document_id": "doc-2"}),
            ("wiki_embedding_units", {"id": "u1"}, None, {"id": "u1", "text": BODY, "source_document_id": "doc-1",
                                                           "embedding_vector_id": "v1"}),
            ("wiki_embedding_vectors", {"id": "v1"}, None, {"id": "v1", "representation_text": BODY}),
            ("pipeline_runs", {"id": "11111111-1111-1111-1111-111111111111"}, None, {"document_id": "doc-1", "manifest": {"x": BODY}}),
            ("wiki_pages", {"id": source_page}, None, {"id": source_page, "summary": BODY}),
            ("__object__", {"bucket": "b", "key": f"wiki/ws/pages/{source_page}/ops/o.md"}, None, {"text": BODY}),
            ("__object__", {"bucket": "b", "key": "wiki/ws/pages/concept/ops/o.md"}, None, {"text": "개념"}),
        ]:
            conn.execute(
                "INSERT INTO ai_task_changes(run_id, table_name, row_key, before_value, after_value) "
                "VALUES ('chat-1', %s, %s, %s, %s)",
                (table, Json(key), Json(before) if before else None, Json(after)),
            )

    _purge()

    with connect() as conn:
        changes = [dict(row) for row in conn.execute("SELECT table_name, row_key FROM ai_task_changes").fetchall()]
        runs = {row["id"]: row for row in conn.execute("SELECT id, command, result FROM ai_task_runs").fetchall()}
    assert {(c["table_name"], c["row_key"].get("document_id") or c["row_key"].get("key")) for c in changes} == {
        ("source_blocks", "doc-2"),
        ("__object__", "wiki/ws/pages/concept/ops/o.md"),
    }
    assert BODY not in str(runs["chat-1"]["command"]) and BODY not in str(runs["chat-1"]["result"])
    assert runs["chat-1"]["command"]["document_id"] == "doc-1"
    assert runs["chat-2"]["command"]["input_markdown"] == "다른 문서"


def test_task_rollback_after_purge_does_not_restore_body(stack) -> None:
    _, _, connect, _ = stack
    _task_run(connect, "ingest-1", "document", {"run_id": "ingest-1", "document_id": "doc-1"}, status="cancel_requested")
    with connect() as conn:
        conn.execute(
            "INSERT INTO ai_task_changes(run_id, table_name, row_key, before_value, after_value) VALUES "
            "('ingest-1', 'source_blocks', %s, %s, NULL)",
            (Json({"block_id": "b1", "document_id": "doc-1"}),
             Json({"block_id": "b1", "document_id": "doc-1", "text": BODY, "position": 0})),
        )

    _purge()
    journal.rollback("ingest-1")

    with connect() as conn:
        assert conn.execute("SELECT count(*) AS c FROM source_blocks").fetchone()["c"] == 0
        assert conn.execute("SELECT status FROM ai_task_runs WHERE id = 'ingest-1'").fetchone()["status"] == "cancelled"


def test_evidence_citing_the_document_only_in_source_refs_is_blanked_and_other_evidence_stays(stack) -> None:
    runs, _, connect, _ = stack
    snippets = [
        # 여러 문서를 걸친 근거는 대표 id가 다른 문서이거나 비어 있다. 문서별로 나눌 수 없어 함께 비운다.
        {"source_document_id": "doc-2", "source_refs": [{"source_document_id": "doc-2"}, {"source_document_id": "doc-1"}],
         "text": BODY, "rank": 1},
        {"source_document_id": None, "source_refs": [{"source_document_id": "doc-1"}, {"source_document_id": "doc-3"}],
         "text": BODY, "rank": 2},
        {"source_document_id": "doc-2", "source_refs": [{"source_document_id": "doc-2"}, {"source_document_id": "doc-3"}],
         "text": "다른 근거", "rank": 3},
        {"source_document_id": "doc-1-copy", "source_refs": [{"source_document_id": "doc-1-copy"}], "text": "비슷한 id", "rank": 4},
    ]
    _task_run(connect, "query-1", "query", {"run_id": "query-1"}, {"answer": "답변", "evidence_snippets": snippets})

    _purge()

    with connect() as conn:
        query = conn.execute("SELECT result FROM ai_task_runs WHERE id = 'query-1'").fetchone()["result"]
    assert query["answer"] == "답변" and BODY not in str(query)
    assert [item["text"] for item in query["evidence_snippets"]] == ["", "", "다른 근거", "비슷한 id"]
    assert query["evidence_snippets"][0]["source_refs"] == snippets[0]["source_refs"]


def test_query_evidence_text_is_removed_but_answer_and_other_documents_stay(stack) -> None:
    runs, _, connect, _ = stack
    snippets = [{"source_document_id": "doc-1", "text": BODY, "rank": 1},
                {"source_document_id": "doc-2", "text": "다른 근거", "rank": 2}]
    _task_run(connect, "query-1", "query", {"run_id": "query-1"}, {"answer": "답변", "evidence_snippets": snippets})
    runs.create_with_planning_job(_workflow_run(), "edit-job")
    with connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET action = 'markdown_turn', document_id = 'doc-1', result = %s WHERE id = %s",
            (Json({"action": "markdown_edit", "edit": {"replacement_markdown": BODY},
                   "generated_markdown": {"markdown": BODY},
                   "chat": {"answer": "답변", "evidence_snippets": snippets}}), RUN_ID),
        )

    _purge()

    with connect() as conn:
        query = conn.execute("SELECT result FROM ai_task_runs WHERE id = 'query-1'").fetchone()["result"]
        turn = conn.execute("SELECT result FROM agent_runs WHERE id = %s", (RUN_ID,)).fetchone()["result"]
    assert query["answer"] == "답변" and BODY not in str(query)
    assert [item["text"] for item in query["evidence_snippets"]] == ["", "다른 근거"]
    assert turn["action"] == "markdown_edit" and "edit" not in turn and "generated_markdown" not in turn
    assert turn["chat"]["answer"] == "답변" and BODY not in str(turn)


def test_document_scoped_tables_are_cleared_and_usage_ledger_stays(stack) -> None:
    _, _, connect, _ = stack
    with connect() as conn:
        conn.execute("INSERT INTO pipeline_runs(id, document_id, workspace_id, input_source, output_dir, mode, status, manifest) "
                     "VALUES ('22222222-2222-2222-2222-222222222222', 'doc-1', 'ws', 'i', 'o', 'm', 'succeeded', %s)",
                     (Json({"post_ingest": {"quality_cases": [{"expected_claims": [BODY]}]}}),))
        conn.execute("INSERT INTO pipeline_runs(id, document_id, workspace_id, input_source, output_dir, mode, status, manifest) "
                     "VALUES ('33333333-3333-3333-3333-333333333333', 'doc-2', 'ws', 'i', 'o', 'm', 'succeeded', %s)",
                     (Json({"keep": True}),))
        conn.execute("INSERT INTO document_quality_reviews(document_id, workspace_id, ingest_run_id, evaluations) "
                     "VALUES ('doc-1', 'ws', '22222222-2222-2222-2222-222222222222', %s)", (Json([{"answer": BODY}]),))
        conn.execute("INSERT INTO source_blocks(document_id, block_id, text) VALUES ('doc-1', 'b', %s), ('doc-2', 'b', 'x')", (BODY,))
        conn.execute("INSERT INTO source_block_snapshots(document_id, source_content_hash) VALUES ('doc-1', 'h')")
        conn.execute("INSERT INTO ai_model_usage(id, run_id, workspace_id, user_id, kind, provider, requested_model, model, status) "
                     "VALUES ('44444444-4444-4444-4444-444444444444', 'r', 'ws', 'user', 'ingest', 'p', 'm', 'm', 'succeeded')")

    _purge()

    with connect() as conn:
        manifests = {row["document_id"]: row["manifest"] for row in conn.execute("SELECT document_id, manifest FROM pipeline_runs")}
        assert BODY not in str(manifests["doc-1"]) and manifests["doc-2"] == {"keep": True}
        assert conn.execute("SELECT count(*) AS c FROM document_quality_reviews").fetchone()["c"] == 0
        assert [row["document_id"] for row in conn.execute("SELECT document_id FROM source_blocks")] == ["doc-2"]
        assert conn.execute("SELECT count(*) AS c FROM source_block_snapshots").fetchone()["c"] == 0
        assert conn.execute("SELECT count(*) AS c FROM ai_model_usage").fetchone()["c"] == 1


def test_workspace_mismatch_on_document_scoped_rows_refuses_to_clear(stack) -> None:
    _, _, connect, _ = stack
    with connect() as conn:
        conn.execute("INSERT INTO pipeline_runs(id, document_id, workspace_id, input_source, output_dir, mode, status) "
                     "VALUES ('55555555-5555-5555-5555-555555555555', 'doc-1', 'other-ws', 'i', 'o', 'm', 'succeeded')")
        conn.execute("INSERT INTO source_blocks(document_id, block_id, text) VALUES ('doc-1', 'b', %s)", (BODY,))

    result = _purge()

    assert result.failed is True
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS c FROM source_blocks").fetchone()["c"] == 1


# ── 지운 뒤 동작: 승인 ───────────────────────────────────────────────


def _approve(runs, operation_ids=None):
    run, plan = runs.get_current_plan_for_user("ws", "user", RUN_ID)
    return ApproveAgentPlanUseCase(runs).execute(
        workspace_id="ws", user_id="user", run_id=RUN_ID, plan_version=plan.version,
        operation_hash=plan.operation_hash, operation_ids=operation_ids,
    )


def test_approving_plan_with_only_purged_document_is_rejected(stack) -> None:
    runs, _, connect, _ = stack
    _awaiting(runs, 1)

    _purge()

    with pytest.raises(ValueError):  # 라우터가 409로 바꾼다.
        _approve(runs)
    with connect() as conn:
        assert conn.execute("SELECT status FROM agent_runs WHERE id = %s", (RUN_ID,)).fetchone()["status"] == "awaiting_approval"
        assert conn.execute("SELECT count(*) AS c FROM agent_jobs WHERE job_type = 'execution'").fetchone()["c"] == 0


def test_approving_multi_document_plan_runs_only_remaining_document(stack) -> None:
    runs, jobs, connect, _ = stack
    _awaiting(runs, 1, 2)

    _purge()
    _approve(runs)

    _, plan = runs.get_current_plan_for_user("ws", "user", RUN_ID)
    statuses = {operation.id: (operation.status, operation.error_code) for operation in plan.operations}
    assert statuses == {"op-1": ("skipped", "document_purged"), "op-2": ("pending", None)}
    assert jobs.mark_operation("op-2", ("pending",), "succeeded")
    assert jobs.mark_run_status(RUN_ID, ("executing",), "verifying")
    jobs.finish_run_from_operations(RUN_ID)
    assert runs.get_for_user("ws", "user", RUN_ID).status == "partial_failed"


def test_selecting_only_purged_operation_is_rejected_and_other_can_be_selected(stack) -> None:
    runs, _, _, _ = stack
    _awaiting(runs, 1, 2)

    _purge()

    with pytest.raises(ValueError):
        _approve(runs, ["op-1"])
    assert _approve(runs, ["op-2"]).status == "executing"


def test_edit_previews_keep_remaining_document_acceptable(stack) -> None:
    runs, _, _, _ = stack
    _awaiting(runs, 1, 2)

    _purge()

    previews = runs.get_edit_previews("ws", "user", RUN_ID)
    assert [item["document_id"] for item in previews["items"]] == ["doc-2"]
    assert previews["expires_at"] > datetime.now(timezone.utc)


def test_revise_with_purged_attachment_is_rejected(stack) -> None:
    runs, _, _, _ = stack
    _awaiting(runs, 1)

    _purge()

    with pytest.raises(ValueError):
        runs.revise("ws", "user", RUN_ID, "다시", "job-2")


# ── 지운 뒤 동작: 되돌리기 ───────────────────────────────────────────


class _Gateway:
    """문서 수정 되돌리기만 흉내 낸다. 지운 문서는 document-svc처럼 404다."""

    def __init__(self, purged: set[str]) -> None:
        self.purged = purged
        self.executed: list[str] = []
        self.current: dict[str, str] = {}

    def read(self, tool, *, arguments, **kwargs):
        document_id = arguments["document_id"]
        if document_id in self.purged:
            raise ToolGatewayError(404, False)
        return {"id": document_id, "edit_revision": 4, "content_hash": self.current[document_id]}

    def execute(self, tool, *, arguments, **kwargs):
        document_id = arguments["document_id"]
        if document_id in self.purged:
            raise ToolGatewayError(404, False)
        self.executed.append(document_id)
        self.current[document_id] = arguments["content_hash"].removeprefix("sha256:")
        return {"document_id": document_id, "current_version": 4, "content_hash": self.current[document_id]}


def _cancel_and_roll_back(runs, jobs, gateway) -> None:
    runs.cancel("ws", "user", RUN_ID)
    RollbackAgentRunUseCase(jobs, runs, gateway).execute(RUN_ID)


def test_rollback_after_purge_restores_other_document_and_reports_purged(stack) -> None:
    runs, jobs, connect, objects = stack
    _execute_two_documents(runs, jobs, connect)
    _purge()
    gateway = _Gateway({"doc-1"})

    _cancel_and_roll_back(runs, jobs, gateway)

    assert gateway.executed == ["doc-2"]
    with connect() as conn:
        run = conn.execute("SELECT status, error_code, finished_at FROM agent_runs WHERE id = %s", (RUN_ID,)).fetchone()
        statuses = {row["id"]: row["status"] for row in conn.execute("SELECT id, status FROM agent_plan_operations")}
    assert (run["status"], run["error_code"]) == ("rollback_failed", "rollback_document_purged")
    assert run["finished_at"] is not None
    assert statuses["op-2"] == "rolled_back"
    assert statuses["op-1"] != "rolled_back"


def test_rollback_retry_after_purge_is_idempotent(stack) -> None:
    runs, jobs, connect, _ = stack
    _execute_two_documents(runs, jobs, connect)
    _purge()
    gateway = _Gateway({"doc-1"})
    _cancel_and_roll_back(runs, jobs, gateway)

    _cancel_and_roll_back(runs, jobs, gateway)

    assert gateway.executed == ["doc-2"]
    with connect() as conn:
        run = conn.execute("SELECT status, error_code FROM agent_runs WHERE id = %s", (RUN_ID,)).fetchone()
    assert (run["status"], run["error_code"]) == ("rollback_failed", "rollback_document_purged")


def test_rollback_of_created_then_purged_document_does_not_call_delete(stack) -> None:
    runs, jobs, connect, _ = stack
    runs.create_with_planning_job(_workflow_run(), "edit-job")
    operation = AgentPlanOperation(
        id="op-create", sequence=1, tool_name="create_document", target_type="document", target_id=None,
        base_version=None, source_parent_id=None, destination_parent_id=None,
        arguments={"display_name": "새 문서"}, reason="새 문서 작성",
    )
    runs.save_plan(RUN_ID, build_agent_plan("edit-plan", RUN_ID, 1, "요약", (operation,)))
    run, plan = runs.get_current_plan_for_user("ws", "user", RUN_ID)
    runs.approve_and_enqueue(run, plan, "approval-1", "execution-job")
    jobs.prepare_undo(RUN_ID, "edit-plan", "op-create", "create_document", {}, {})
    jobs.save_tool_execution(
        run_id=RUN_ID, plan_id="edit-plan", operation_id="op-create", tool_name="create_document",
        idempotency_key=f"agent:{RUN_ID}:edit-plan:op-create", attempt=1, status="succeeded",
        response_metadata={"id": "created-doc", "current_version": 1}, error_code=None,
    )
    with connect() as conn:
        conn.execute("UPDATE agent_plan_operations SET status = 'succeeded'")
    _purge("created-doc")
    gateway = _Gateway({"created-doc"})

    _cancel_and_roll_back(runs, jobs, gateway)

    assert gateway.executed == []
    with connect() as conn:
        assert conn.execute("SELECT error_code FROM agent_runs WHERE id = %s", (RUN_ID,)).fetchone()["error_code"] \
            == "rollback_document_purged"


def test_parent_turn_reports_purged_child(stack) -> None:
    runs, jobs, connect, _ = stack
    runs.create_with_planning_job(_workflow_run(), "edit-job")
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_runs(id, workspace_id, user_id, action, status, request_summary, result) "
            "VALUES ('parent', 'ws', 'user', 'markdown_turn', 'rolling_back', '요청', %s)",
            (Json({"run_id": RUN_ID}),),
        )
        conn.execute("UPDATE agent_runs SET status = 'rollback_failed', error_code = 'rollback_document_purged' "
                     "WHERE id = %s", (RUN_ID,))

    assert jobs.parent_rollback_error("parent") == "rollback_document_purged"


def test_purge_rollback_failed_run_becomes_eligible_for_90_day_cleanup(stack) -> None:
    runs, jobs, connect, _ = stack
    runs.create_with_planning_job(_workflow_run(), "edit-job")
    runs.create_with_planning_job(
        AgentRun(id="other-failure", workspace_id="ws", user_id="user", action="folder_organize",
                 skill_version_id=None, status="queued", request_summary="요청"),
        "other-job",
    )
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status = 'rollback_failed', error_code = 'rollback_document_purged', "
                     "finished_at = now() - interval '91 days' WHERE id = %s", (RUN_ID,))
        conn.execute("UPDATE agent_runs SET status = 'rollback_failed', error_code = 'rollback_conflict', "
                     "finished_at = now() - interval '91 days' WHERE id = 'other-failure'")

    assert jobs.list_expired_run_ids() == (RUN_ID,)
    assert jobs.delete_expired_runs((RUN_ID, "other-failure")) == 1


# ── 90일 run 정리 ───────────────────────────────────────────────────


def test_expired_run_objects_are_deleted_by_run_prefix(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.modules.agent_run.infrastructure.postgres_agent_run_repository import _run_object_prefix
    from app.modules.data_purge.infrastructure import postgres_purge

    keep = _run_object_prefix("other") + "artifacts/a.md"
    gone = _run_object_prefix("expired") + "artifacts/a.md"

    class Storage:
        objects = {keep, gone}

        def list_objects(self, bucket, prefix, recursive):
            return [SimpleNamespace(object_name=key) for key in self.objects if key.startswith(prefix)]

        def remove_object(self, bucket, key):
            self.objects.discard(key)

    monkeypatch.setattr(postgres_purge.object_storage, "client", Storage)

    assert postgres_purge.delete_run_objects("expired") == 1
    assert Storage.objects == {keep}


def test_expired_run_with_failed_object_delete_keeps_row_for_next_cycle(monkeypatch) -> None:
    import agent_worker

    repository = MagicMock()
    checkpointer = MagicMock()
    repository.list_expired_run_ids.return_value = ("bad", "good")
    repository.delete_expired_runs.return_value = 1

    def delete_objects(run_id):
        if run_id == "bad":
            raise OSError("storage down")
        return 1

    monkeypatch.setattr(agent_worker, "delete_run_objects", delete_objects)

    agent_worker._cleanup_expired_runs_if_due(repository, checkpointer, 0.0, 100.0)

    repository.delete_expired_runs.assert_called_once_with(("good",))
    checkpointer.delete_thread.assert_called_once_with("good")


def test_full_expired_batch_runs_again_soon(monkeypatch) -> None:
    import agent_worker

    repository = MagicMock()
    repository.list_expired_run_ids.return_value = tuple(f"run-{i}" for i in range(agent_worker._EXPIRED_RUN_BATCH_SIZE))
    repository.delete_expired_runs.return_value = agent_worker._EXPIRED_RUN_BATCH_SIZE
    monkeypatch.setattr(agent_worker, "delete_run_objects", lambda run_id: 0)

    next_at = agent_worker._cleanup_expired_runs_if_due(repository, MagicMock(), 0.0, 100.0)

    assert next_at == 100.0 + agent_worker._EXPIRED_RUN_BACKLOG_INTERVAL_SECONDS


def test_expired_run_sql_respects_limit(stack) -> None:
    runs, jobs, connect, _ = stack
    runs.create_with_planning_job(_workflow_run(), "edit-job")
    runs.create_with_planning_job(
        AgentRun(id="other", workspace_id="ws", user_id="user", action="folder_organize",
                 skill_version_id=None, status="queued", request_summary="요청"),
        "other-job",
    )
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status = 'failed', finished_at = now() - interval '91 days'")

    assert len(jobs.list_expired_run_ids(limit=1)) == 1
    assert len(jobs.list_expired_run_ids(limit=5)) == 2


# ── 파기 경로 ───────────────────────────────────────────────────────


def test_purge_workspace_removes_purge_requests(monkeypatch, stack) -> None:
    from app.modules.data_purge.infrastructure import postgres_purge

    _, _, connect, _ = stack
    monkeypatch.setattr(postgres_purge, "connect", connect)
    monkeypatch.setattr(postgres_purge, "_delete_objects", lambda prefixes, keys: 0)
    with connect() as conn:
        conn.execute("INSERT INTO ai_document_purges(document_id, workspace_id) VALUES ('doc-1', 'ws'), ('doc-2', 'other')")

    deleted = postgres_purge.purge_workspace("ws")

    assert deleted["ai_document_purges"] == 1
    with connect() as conn:
        assert [row["document_id"] for row in conn.execute("SELECT document_id FROM ai_document_purges")] == ["doc-2"]


def test_wiki_data_of_other_tables_is_unchanged_by_purge(stack) -> None:
    """위키 파생(개념 페이지·tombstone)은 document_deleted 처리에 맡기고 여기서는 건드리지 않는다."""
    _, _, connect, _ = stack
    with connect() as conn:
        conn.execute("INSERT INTO wiki_source_tombstones(workspace_id, document_id) VALUES ('ws', 'doc-1')")
        conn.execute(
            "INSERT INTO wiki_pages(id, page_type, title, slug, user_id, workspace_id, status, created_at, updated_at) "
            "VALUES ('concept', 'concept', '개념', 'concept', 'user', 'ws', 'active', now(), now())"
        )

    _purge()

    with connect() as conn:
        assert conn.execute("SELECT count(*) AS c FROM wiki_source_tombstones").fetchone()["c"] == 1
        assert conn.execute("SELECT status FROM wiki_pages WHERE id = 'concept'").fetchone()["status"] == "active"


# ── 위키 소스 페이지 ────────────────────────────────────────────────


def _source_page(connect, page_id: str, document_id: str, status: str = "deleted", workspace_id: str = "ws") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id, page_type, title, slug, summary, markdown_uri, user_id, workspace_id, status, "
            "created_at, updated_at) VALUES (%s, 'source', '제목', %s, %s, %s, 'user', %s, %s, now(), now())",
            (page_id, slugify(document_id), BODY, f"s3://bucket/wiki/{workspace_id}/sources/{document_id}.md",
             workspace_id, status),
        )


def _seed_source_objects(objects: WikiObjects) -> None:
    objects.keys |= {
        "wiki/ws/sources/doc-1.md",
        "wiki/ws/pages/S1/ops/op-1.md",
        "wiki/ws/pages/S1/ops/op-2.md",
        "wiki/ws/sources/doc-2.md",
        "wiki/ws/pages/S2/ops/op-1.md",
        "wiki/ws/pages/concept/ops/op-1.md",
        "wiki/ws/pages/concept/ops/op-1.json",
        "wiki/ws/clusters/active.md",
    }


def test_source_page_objects_and_summary_are_removed_and_others_stay(stack, wiki_objects) -> None:
    _, _, connect, _ = stack
    _source_page(connect, "S1", "doc-1")
    _source_page(connect, "S2", "doc-2")
    _seed_source_objects(wiki_objects)
    with connect() as conn:
        conn.execute("INSERT INTO wiki_source_tombstones(workspace_id, document_id) VALUES ('ws', 'doc-1')")

    assert _purge().finished

    assert wiki_objects.keys == {
        "wiki/ws/sources/doc-2.md",
        "wiki/ws/pages/S2/ops/op-1.md",
        "wiki/ws/pages/concept/ops/op-1.md",
        "wiki/ws/pages/concept/ops/op-1.json",
        "wiki/ws/clusters/active.md",
    }
    with connect() as conn:
        pages = {row["id"]: row for row in conn.execute("SELECT id, status, summary, markdown_uri FROM wiki_pages")}
        tombstones = conn.execute("SELECT count(*) AS c FROM wiki_source_tombstones").fetchone()["c"]
    # 행은 남긴다. 로그 되돌리기가 page id로 찾고, tombstone·슬러그 고유 제약도 그대로 둔다.
    assert (pages["S1"]["status"], pages["S1"]["summary"], pages["S1"]["markdown_uri"]) == ("deleted", "", None)
    assert pages["S2"]["summary"] == BODY and pages["S2"]["markdown_uri"] is not None
    assert tombstones == 1


def test_source_object_failure_is_retried_on_same_path(stack, wiki_objects) -> None:
    _, _, connect, _ = stack
    _source_page(connect, "S1", "doc-1")
    _seed_source_objects(wiki_objects)
    wiki_objects.failing = True

    result = _purge()

    assert not result.finished and result.pending_source_objects == 1
    with connect() as conn:
        assert conn.execute("SELECT summary FROM wiki_pages WHERE id = 'S1'").fetchone()["summary"] == ""
        assert conn.execute("SELECT finished_at FROM ai_document_purges").fetchone()["finished_at"] is None

    wiki_objects.failing = False
    assert purge.purge_pending_documents().finished == 1
    assert "wiki/ws/sources/doc-1.md" not in wiki_objects.keys
    assert "wiki/ws/pages/S1/ops/op-1.md" not in wiki_objects.keys


def test_active_source_page_is_retired_first_when_document_deleted_was_lost(stack, wiki_objects, monkeypatch) -> None:
    _, _, connect, _ = stack
    _source_page(connect, "S1", "doc-1", status="active")
    _seed_source_objects(wiki_objects)
    retired = []

    def retire(workspace_id, document_id):
        retired.append((workspace_id, document_id))
        with connect() as conn:
            conn.execute("UPDATE wiki_pages SET status = 'deleted' WHERE id = 'S1'")

    monkeypatch.setattr(purge.database, "delete_document_wiki_data", retire)

    assert _purge().finished
    assert retired == [("ws", "doc-1")]


def test_concept_lock_timeout_on_retire_stays_pending(stack, wiki_objects, monkeypatch) -> None:
    from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import ConceptLockTimeout

    _, _, connect, _ = stack
    _source_page(connect, "S1", "doc-1", status="active")
    monkeypatch.setattr(purge.database, "delete_document_wiki_data", MagicMock(side_effect=ConceptLockTimeout("x")))

    assert _purge().failed is True
    with connect() as conn:
        assert conn.execute("SELECT finished_at FROM ai_document_purges").fetchone()["finished_at"] is None


def test_source_page_purged_lookup(stack) -> None:
    _, _, connect, _ = stack
    _source_page(connect, "S1", "doc-1")
    _source_page(connect, "S2", "doc-2")

    _purge()

    assert purge.is_source_page_purged("ws", "S1") is True
    assert purge.is_source_page_purged("ws", "S2") is False
    assert purge.is_source_page_purged("other", "S1") is False


# ── document가 본문을 지운 command로 취소할 때 ──────────────────────


def _cancel_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.modules.task_cancellation.interfaces.http.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_cancel_with_purged_command_skips_hash_check_and_rolls_back(stack) -> None:
    runs, jobs, connect, _ = stack
    _execute_two_documents(runs, jobs, connect)
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET command_envelope_hash = 'original' WHERE id = %s", (RUN_ID,))

    response = _cancel_client().post(f"/internal/ai/tasks/{RUN_ID}/cancel", json={
        "workspace_id": "ws", "user_id": "user",
        "command": {"run_id": RUN_ID, "workspace_id": "ws", "user_id": "user", "kind": "agent", "document_purged": True},
    })

    assert response.status_code == 200 and response.json()["status"] == "cancel_requested"


def test_cancel_with_changed_command_without_marker_is_still_rejected(stack) -> None:
    _, _, connect, _ = stack
    _task_run(connect, "task-1", "chat_wiki", {"run_id": "task-1"})
    with connect() as conn:
        conn.execute("UPDATE ai_task_runs SET command_hash = 'original' WHERE id = 'task-1'")

    response = _cancel_client().post("/internal/ai/tasks/task-1/cancel", json={
        "workspace_id": "ws", "user_id": "user",
        "command": {"run_id": "task-1", "workspace_id": "ws", "user_id": "user", "kind": "chat_wiki"},
    })

    assert response.status_code == 409


def test_cancel_journal_task_with_purged_command_skips_registration(stack) -> None:
    _, _, connect, _ = stack
    _task_run(connect, "task-1", "chat_wiki", {"run_id": "task-1", "document_id": "doc-1"})
    with connect() as conn:
        conn.execute("UPDATE ai_task_runs SET command_hash = 'original' WHERE id = 'task-1'")

    with patch.object(journal, "rollback_pending"):
        response = _cancel_client().post("/internal/ai/tasks/task-1/cancel", json={
            "workspace_id": "ws", "user_id": "user",
            "command": {"run_id": "task-1", "workspace_id": "ws", "user_id": "user", "kind": "chat_wiki",
                        "document_purged": True},
        })

    assert response.status_code == 200


def test_cancel_unknown_task_with_purged_command_is_conflict_not_retry_loop(stack) -> None:
    response = _cancel_client().post("/internal/ai/tasks/missing/cancel", json={
        "workspace_id": "ws", "user_id": "user",
        "command": {"run_id": "missing", "workspace_id": "ws", "user_id": "user", "kind": "chat_wiki",
                    "document_purged": True},
    })

    assert response.status_code == 409


def test_result_reports_only_this_documents_source_pages_even_when_objects_fail(stack, wiki_objects) -> None:
    _, _, connect, _ = stack
    _source_page(connect, "S1", "doc-1")
    _source_page(connect, "S2", "doc-2")
    with connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id, page_type, title, slug, user_id, workspace_id, status, created_at, updated_at) "
            "VALUES ('concept', 'concept', '개념', 'doc-1', 'user', 'ws', 'active', now(), now())"
        )
    wiki_objects.failing = True

    result = _purge()

    assert result.source_page_ids == ("S1",) and not result.finished


def test_unexpected_failure_still_reports_source_pages(stack, monkeypatch) -> None:
    _, _, connect, _ = stack
    _source_page(connect, "S1", "doc-1")
    monkeypatch.setattr(purge, "_scrub_rows", MagicMock(side_effect=RuntimeError("boom")))

    result = _purge()

    assert result.failed is True and result.source_page_ids == ("S1",)
