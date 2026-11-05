"""TEST_AGENT_DATABASE_URL을 지정하면 격리 스키마에 현행 ai_schema.sql을 적용해 실제 저장소를 검증한다."""

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.modules.agent_run.domain.entities import AgentRun
from app.modules.agent_run.domain.plan import AgentPlanOperation, build_agent_plan


@pytest.fixture
def repositories(monkeypatch) -> Iterator[tuple[object, object, object]]:
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository
    from app.modules.agent_run.infrastructure.postgres_agent_run_repository import PostgresAgentRunRepository
    from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

    schema = "test_agent_run_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-csearch_path={schema}")

    schema_sql = Path(__file__).resolve().parents[3].joinpath("db/ai_schema.sql").read_text()
    with connect() as conn:
        conn.execute(schema_sql)
        # 같은 DDL을 두 번 적용해도 실패하지 않아야 한다(멱등 migration).
        conn.execute(schema_sql)
    monkeypatch.setattr(database, "connect_ai", connect)
    try:
        yield PostgresAgentRunRepository(), PostgresAgentJobRepository(), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _operation(plan_id: str, sequence: int, depends_on: tuple[int, ...] = ()) -> AgentPlanOperation:
    return AgentPlanOperation(
        id=f"{plan_id}-op-{sequence}",
        sequence=sequence,
        tool_name="create_folder",
        target_type="folder",
        target_id=None,
        base_version=None,
        source_parent_id=None,
        destination_parent_id=None,
        arguments={"name": f"폴더 {sequence}", "parent_folder_id": None},
        reason="정리",
        depends_on=tuple(f"{plan_id}-op-{dependency}" for dependency in depends_on),
        warning={"type": "duplicate_name", "message": "중복일 수 있어요."} if sequence == 3 else None,
        suggested_selected=sequence != 3,
    )


def _awaiting_run(runs, *, target_folder_id: str | None = None, rule_summary: str | None = None):
    run = runs.create_with_planning_job(
        AgentRun(
            id="run-1",
            workspace_id="ws",
            user_id="user",
            action="folder_organize",
            skill_version_id=None,
            status="queued",
            request_summary="정리",
            target_folder_id=target_folder_id,
            rule_summary=rule_summary,
        ),
        "planning-job",
    )
    plan = build_agent_plan(
        "plan-1", run.id, 1, "정리",
        (_operation("plan-1", 1), _operation("plan-1", 2, (1,)), _operation("plan-1", 3)),
    )
    runs.save_plan(run.id, plan)
    return runs.get_current_plan_for_user("ws", "user", run.id)


def test_partial_approval_skips_unselected_operations_and_completes(repositories) -> None:
    runs, jobs, connect = repositories
    run, plan = _awaiting_run(runs)

    runs.approve_and_enqueue(run, plan, "approval-1", "execution-job", ("plan-1-op-1", "plan-1-op-2"))

    _, approved = runs.get_current_plan_for_user("ws", "user", run.id)
    statuses = {operation.id: (operation.status, operation.error_code) for operation in approved.operations}
    assert statuses == {
        "plan-1-op-1": ("pending", None),
        "plan-1-op-2": ("pending", None),
        "plan-1-op-3": ("skipped", "not_selected"),
    }
    with connect() as conn:
        approval = conn.execute("SELECT operation_ids FROM agent_approvals WHERE id = 'approval-1'").fetchone()
    assert approval["operation_ids"] == ["plan-1-op-1", "plan-1-op-2"]

    for operation_id in ("plan-1-op-1", "plan-1-op-2"):
        assert jobs.mark_operation(operation_id, ("pending",), "succeeded")
    assert jobs.mark_run_status(run.id, ("executing",), "verifying")
    jobs.finish_run_from_operations(run.id)

    # 고르지 않은 작업은 실패가 아니라 run은 completed다.
    assert runs.get_for_user("ws", "user", run.id).status == "completed"


def test_partial_approval_still_reports_selected_failures(repositories) -> None:
    runs, jobs, _ = repositories
    run, plan = _awaiting_run(runs)

    runs.approve_and_enqueue(run, plan, "approval-1", "execution-job", ("plan-1-op-1", "plan-1-op-2"))
    assert jobs.mark_operation("plan-1-op-1", ("pending",), "succeeded")
    assert jobs.mark_operation("plan-1-op-2", ("pending",), "failed", "tool_http_500")
    assert jobs.mark_run_status(run.id, ("executing",), "verifying")
    jobs.finish_run_from_operations(run.id)

    assert runs.get_for_user("ws", "user", run.id).status == "partial_failed"


def test_full_approval_keeps_every_operation_pending(repositories) -> None:
    runs, _, connect = repositories
    run, plan = _awaiting_run(runs)

    runs.approve_and_enqueue(run, plan, "approval-1", "execution-job")

    _, approved = runs.get_current_plan_for_user("ws", "user", run.id)
    assert {operation.status for operation in approved.operations} == {"pending"}
    with connect() as conn:
        approval = conn.execute("SELECT operation_ids FROM agent_approvals WHERE id = 'approval-1'").fetchone()
    assert approval["operation_ids"] is None


def test_approver_is_recorded_and_returned_with_the_plan(repositories) -> None:
    runs, _, connect = repositories
    run, plan = _awaiting_run(runs)

    _, awaiting = runs.get_current_plan_for_user("ws", "user", run.id)
    assert awaiting.approved_by is None
    runs.approve_and_enqueue(run, plan, "approval-1", "execution-job", approved_by="auto:user-setting")

    _, approved = runs.get_current_plan_for_user("ws", "user", run.id)
    assert approved.approved_by == "auto:user-setting"
    with connect() as conn:
        approval = conn.execute("SELECT approved_by, user_id FROM agent_approvals WHERE id = 'approval-1'").fetchone()
    # 자동 승인도 그 설정을 켠 사용자의 승인으로 남는다.
    assert approval == {"approved_by": "auto:user-setting", "user_id": "user"}


def test_manual_approval_is_reported_as_user(repositories) -> None:
    runs, _, _ = repositories
    run, plan = _awaiting_run(runs)

    runs.approve_and_enqueue(run, plan, "approval-1", "execution-job")

    _, approved = runs.get_current_plan_for_user("ws", "user", run.id)
    assert approved.approved_by == "user"


def test_target_folder_is_stored_and_returned_with_the_run(repositories) -> None:
    from app.modules.agent_run.interfaces.http.schemas import AgentRunResponse

    runs, jobs, _ = repositories
    run, plan = _awaiting_run(runs, target_folder_id="folder-9")

    assert run.target_folder_id == "folder-9"
    assert jobs.load_context(run.id).run.target_folder_id == "folder-9"
    body = AgentRunResponse.from_domain(run, plan).model_dump()
    assert body["target_folder_id"] == "folder-9"
    approved = runs.approve_and_enqueue(run, plan, "approval-1", "execution-job")
    assert AgentRunResponse.from_domain(approved).model_dump()["target_folder_id"] == "folder-9"


def test_plan_review_metadata_and_rule_summary_round_trip(repositories) -> None:
    from app.modules.agent_run.interfaces.http.schemas import AgentRunResponse

    runs, _, _ = repositories
    run, plan = _awaiting_run(runs, rule_summary="날짜_분류_제목 형식으로 이름을 바꾼다.")

    body = AgentRunResponse.from_domain(run, plan).model_dump()
    assert body["rule_summary"] == "날짜_분류_제목 형식으로 이름을 바꾼다."
    operations = {operation["id"]: operation for operation in body["plan"]["operations"]}
    assert operations["plan-1-op-1"]["warning"] is None
    assert operations["plan-1-op-1"]["suggested_selected"] is True
    assert operations["plan-1-op-3"]["warning"] == {"type": "duplicate_name", "message": "중복일 수 있어요."}
    assert operations["plan-1-op-3"]["suggested_selected"] is False
    # 검토 메타데이터는 해시에 들지 않아, 메타데이터가 다른 같은 계획도 해시가 같다.
    stripped = build_agent_plan(
        "plan-1", run.id, 1, "정리",
        tuple(replace(operation, warning=None, suggested_selected=True) for operation in plan.operations),
    )
    assert stripped.operation_hash == plan.operation_hash


_TARGET_HIERARCHY = [
    {"id": "folder-9", "type": "folder", "current_version": 1, "parent_id": None},
    {"id": "document-1", "type": "document", "current_version": 3, "parent_id": "folder-9"},
]
_RENAME_TARGET_FOLDER = {
    "tool_name": "rename_folder", "target_type": "folder", "target_id": "folder-9", "base_version": 1,
    "source_parent_id": None, "destination_parent_id": None,
    "arguments": {"folder_id": "folder-9", "name": "정리됨", "base_version": 1},
    "reason": "이름을 바꿉니다.", "depends_on": [],
}
_RENAME_DOCUMENT = {
    "tool_name": "rename_document", "target_type": "document", "target_id": "document-1", "base_version": 3,
    "source_parent_id": None, "destination_parent_id": None,
    "arguments": {"document_id": "document-1", "display_name": "회의록.md", "base_version": 3},
    "reason": "이름 형식을 맞춥니다.", "depends_on": [],
}


def _plan_with_target_folder(runs, jobs, operations):
    """대상 폴더를 고른 정리 run의 planning job을 실제 worker로 한 번 처리하고 모델 호출 수를 돌려준다."""
    from unittest.mock import MagicMock, patch

    from app.modules.agent_run.infrastructure.agent_worker import AgentWorker
    from app.modules.agent_run.infrastructure.chat_completions_plan_generator import ChatCompletionsPlanGenerator

    runs.create_with_planning_job(
        AgentRun(
            id="run-1", workspace_id="ws", user_id="user", action="folder_organize", skill_version_id=None,
            status="queued", request_summary="정리", target_folder_id="folder-9",
        ),
        "planning-job",
    )
    client = MagicMock()
    client.complete_json.return_value = {"intent_confirmed": True, "summary": "폴더를 정리합니다.", "operations": operations}
    worker = AgentWorker(jobs, runs, MagicMock(), ChatCompletionsPlanGenerator(client, "system"))
    job = jobs.claim_next("worker-1")
    with patch.object(worker, "_inspect_hierarchy", return_value=_TARGET_HIERARCHY):
        worker.process(job)
    return client.complete_json.call_count


def test_target_folder_change_is_dropped_and_explained_in_saved_plan(repositories) -> None:
    runs, jobs, _ = repositories

    _plan_with_target_folder(runs, jobs, [_RENAME_TARGET_FOLDER, _RENAME_DOCUMENT])

    run, plan = runs.get_current_plan_for_user("ws", "user", "run-1")
    assert run.status == "awaiting_approval"
    assert run.error_code is None
    assert [operation.tool_name for operation in plan.operations] == ["rename_document"]
    assert plan.summary == "폴더를 정리합니다. 대상 폴더 자체는 바꿀 수 없어 그 폴더의 이름 변경·이동 작업 1개는 계획에서 뺐어요."


def test_target_folder_only_plan_fails_once_with_out_of_scope_code(repositories) -> None:
    runs, jobs, connect = repositories

    calls = _plan_with_target_folder(runs, jobs, [_RENAME_TARGET_FOLDER])

    # 다시 만들어도 같은 결과라 재시도하지 않는다. 모델은 한 번만 부르고 run은 전용 code로 실패한다.
    assert calls == 1
    run = runs.get_for_user("ws", "user", "run-1")
    assert (run.status, run.error_code) == ("failed", "plan_out_of_scope")
    with connect() as conn:
        job = conn.execute("SELECT status, attempt_count FROM agent_jobs WHERE id = 'planning-job'").fetchone()
    assert (job["status"], job["attempt_count"]) == ("failed", 1)
    assert jobs.claim_next("worker-2") is None


def test_revise_gives_the_new_plan_version_a_fresh_tool_budget(repositories) -> None:
    runs, jobs, connect = repositories
    run, _ = _awaiting_run(runs)
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET tool_call_count = 40 WHERE id = %s", (run.id,))
    assert jobs.remaining_tool_calls(run.id) == 0

    runs.revise("ws", "user", run.id, "다시 정리", "planning-job-2")

    assert jobs.remaining_tool_calls(run.id) == 40


def _edit_artifact(index: int) -> "StartAgentRunArtifact":
    from app.modules.agent_run.domain.edit_preview import build_edit_preview
    from app.modules.agent_run.domain.entities import StartAgentRunArtifact
    import hashlib, json

    markdown = f"문서 {index} 수정"
    return StartAgentRunArtifact(
        id=f"zz-artifact-{9 - index}",  # id 정렬과 첨부 순서가 다르다.
        content_hash="sha256:" + hashlib.sha256(markdown.encode()).hexdigest(),
        markdown=markdown,
        purpose="apply_document_edit",
        document_id=f"doc-{index}",
        base_version=index + 1,
        target={"type": "whole_document", "start_line": 1, "end_line": 1},
        display_name=f"제목 {index}",
        summary=f"요약 {index}",
        preview_json=json.dumps(build_edit_preview(f"문서 {index}", markdown), ensure_ascii=False),
    )


def _workflow_run() -> AgentRun:
    return AgentRun(
        id="edit-run", workspace_id="ws", user_id="user", action="workspace_workflow", skill_version_id=None,
        status="queued", request_summary="첨부를 고쳐줘",
    )


@pytest.fixture
def storage(monkeypatch) -> dict[str, str]:
    from app.modules.agent_run.infrastructure import postgres_agent_run_repository as module

    objects: dict[str, str] = {}
    monkeypatch.setattr(module, "write_text_object", lambda key, text, *args, **kwargs: objects.__setitem__(key, text))
    monkeypatch.setattr(module, "read_text_object", lambda key: objects[key])
    monkeypatch.setattr(module, "delete_object", lambda key: objects.pop(key, None))
    return objects


def _plan_for(runs, artifacts) -> None:
    operations = tuple(
        AgentPlanOperation(
            id=f"op-{sequence}", sequence=sequence, tool_name="apply_document_edit", target_type="document",
            target_id=artifact.document_id, base_version=artifact.base_version, source_parent_id=None,
            destination_parent_id=None,
            arguments={"document_id": artifact.document_id, "base_version": artifact.base_version,
                       "target": artifact.target, "content_artifact_id": artifact.id,
                       "content_hash": artifact.content_hash},
            reason=artifact.summary,
        )
        for sequence, artifact in enumerate(artifacts, start=1)
    )
    runs.save_plan("edit-run", build_agent_plan("edit-plan", "edit-run", 1, "요약", operations))


def test_document_edit_artifacts_keep_attachment_order_and_preview_columns(repositories, storage) -> None:
    runs, _, connect = repositories
    created = tuple(_edit_artifact(index) for index in (1, 2, 3))

    runs.create_with_planning_job(_workflow_run(), "edit-job", created)

    loaded = runs.load_document_edit_artifacts("edit-run")
    assert [artifact.document_id for artifact in loaded] == ["doc-1", "doc-2", "doc-3"]
    assert (loaded[0].summary, loaded[0].base_version, loaded[0].expired) == ("요약 1", 2, False)
    assert len(storage) == 6
    with connect() as conn:
        row = conn.execute(
            "SELECT display_name, preview_object_key, expires_at > now() + interval '23 hours' AS day "
            "FROM agent_run_artifacts WHERE document_id = 'doc-2'"
        ).fetchone()
    assert row["display_name"] == "제목 2" and row["preview_object_key"].startswith("agent-runs/") and row["day"]


def test_artifacts_without_preview_are_not_document_edit_artifacts(repositories, storage) -> None:
    runs, _, _ = repositories

    runs.create_with_planning_job(_workflow_run(), "edit-job", (replace(_edit_artifact(1), preview_json=None),))

    assert runs.load_document_edit_artifacts("edit-run") == ()


def test_failed_run_creation_removes_every_written_object(repositories, storage, monkeypatch) -> None:
    runs, _, _ = repositories
    broken = replace(_edit_artifact(2), content_hash="sha256:" + "0" * 64)

    with pytest.raises(ValueError):
        runs.create_with_planning_job(_workflow_run(), "edit-job", (_edit_artifact(1), broken))

    assert storage == {}


def test_edit_previews_follow_plan_order_and_actor(repositories, storage) -> None:
    runs, _, connect = repositories
    created = (_edit_artifact(1), _edit_artifact(2))
    runs.create_with_planning_job(_workflow_run(), "edit-job", created)
    assert runs.get_edit_previews("ws", "user", "edit-run") == {"items": [], "expires_at": None}
    _plan_for(runs, created)

    previews = runs.get_edit_previews("ws", "user", "edit-run")

    assert [item["operation_id"] for item in previews["items"]] == ["op-1", "op-2"]
    first = previews["items"][0]
    assert (first["document_id"], first["display_name"], first["base_version"], first["summary"]) == (
        "doc-1", "제목 1", 2, "요약 1",
    )
    assert (first["added_lines"], first["removed_lines"]) == (1, 1)
    assert previews["expires_at"] is not None
    assert runs.get_edit_previews("ws", "other", "edit-run") is None
    assert runs.get_edit_previews("other", "user", "edit-run") is None
    assert runs.get_edit_previews("ws", "user", "missing") is None


def test_expired_previews_stay_readable_with_past_expiry_but_artifacts_report_expired(repositories, storage) -> None:
    runs, _, connect = repositories
    created = (_edit_artifact(1),)
    runs.create_with_planning_job(_workflow_run(), "edit-job", created)
    _plan_for(runs, created)
    with connect() as conn:
        conn.execute("UPDATE agent_run_artifacts SET expires_at = now() - interval '1 hour'")

    previews = runs.get_edit_previews("ws", "user", "edit-run")

    assert len(previews["items"]) == 1
    assert previews["expires_at"] is not None
    assert runs.load_document_edit_artifacts("edit-run")[0].expired is True
    assert runs.list_artifacts("ws", "user", "edit-run") == []


def test_other_artifact_readers_do_not_expose_preview_columns(repositories, storage) -> None:
    runs, _, _ = repositories
    runs.create_with_planning_job(_workflow_run(), "edit-job", (_edit_artifact(1),))

    listed = runs.list_artifacts("ws", "user", "edit-run")

    assert set(listed[0]) == {"id", "content_hash", "purpose", "document_id", "base_version", "target"}


def test_one_conflict_makes_the_run_conflicted_and_failure_makes_it_partial(repositories, storage) -> None:
    runs, jobs, connect = repositories
    created = (_edit_artifact(1), _edit_artifact(2))
    runs.create_with_planning_job(_workflow_run(), "edit-job", created)
    _plan_for(runs, created)

    def finish(*statuses: str) -> str:
        with connect() as conn:
            conn.execute("UPDATE agent_runs SET status = 'verifying' WHERE id = 'edit-run'")
            for index, status in enumerate(statuses, start=1):
                conn.execute("UPDATE agent_plan_operations SET status = %s WHERE id = %s", (status, f"op-{index}"))
        jobs.finish_run_from_operations("edit-run")
        with connect() as conn:
            return conn.execute("SELECT status FROM agent_runs WHERE id = 'edit-run'").fetchone()["status"]

    assert finish("succeeded", "failed") == "partial_failed"
    assert finish("succeeded", "forbidden") == "partial_failed"
    assert finish("failed", "failed") == "failed"
    assert finish("succeeded", "conflicted") == "conflicted"


def test_rollback_skips_operation_whose_snapshot_read_failed(repositories, storage) -> None:
    runs, jobs, connect = repositories
    created = (_edit_artifact(1),)
    runs.create_with_planning_job(_workflow_run(), "edit-job", created)
    _plan_for(runs, created)
    with connect() as conn:
        conn.execute("UPDATE agent_plan_operations SET status = 'failed', error_code = 'tool_http_404' WHERE id = 'op-1'")

    assert jobs.load_undo_records("edit-run") == []
