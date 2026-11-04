"""만료된 수정안 artifact 정리를 실제 PostgreSQL로 검증한다. 객체 저장소만 가짜다.

TEST_AGENT_DATABASE_URL을 지정해야 돈다. 경합 시나리오는 별도 연결로 실제 행 잠금을 잡는다.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from minio.error import S3Error

from app.modules.agent_run.application.approve_agent_plan import ApproveAgentPlanUseCase
from app.modules.agent_run.infrastructure.postgres_agent_run_repository import (
    _artifact_object_key,
    _preview_object_key,
)
from app.modules.agent_run.interfaces.http.dependencies import (
    get_agent_run_repository,
    get_approve_agent_plan_use_case,
)
from app.modules.agent_run.interfaces.http.routes import router
from tests.modules.agent_run.test_agent_run_postgres import (  # noqa: F401
    _edit_artifact,
    _plan_for,
    _workflow_run,
    repositories,
)

GRACE = 24 * 60 * 60
RUN_ID = "edit-run"


def _s3_error(code: str) -> S3Error:
    return S3Error(MagicMock(), code, "요청 실패", "resource", "request", "host")


class FakeStorage:
    """삭제 호출을 기록하고 키별 실패·대기를 끼워 넣을 수 있는 객체 저장소."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.deleted: list[str] = []
        self.failing: set[str] = set()
        self.on_delete: Callable[[str], None] = lambda key: None

    def read(self, key: str) -> str:
        if key not in self.objects:
            raise _s3_error("NoSuchKey")
        return self.objects[key]

    def write(self, key: str, text: str, *args: object, **kwargs: object) -> None:
        self.objects[key] = text

    def delete(self, key: str) -> None:
        self.on_delete(key)
        if key in self.failing:
            raise _s3_error("InternalError")
        self.deleted.append(key)
        self.objects.pop(key, None)


@pytest.fixture
def storage(monkeypatch) -> FakeStorage:
    from app.modules.agent_run.infrastructure import postgres_agent_run_repository as module

    fake = FakeStorage()
    monkeypatch.setattr(module, "write_text_object", fake.write)
    monkeypatch.setattr(module, "read_text_object", fake.read)
    monkeypatch.setattr(module, "delete_object", fake.delete)
    return fake


def _cleanup(**kwargs: object):
    from app.modules.agent_run.infrastructure.expired_artifact_cleanup import delete_expired_artifacts

    return delete_expired_artifacts(**{"grace_seconds": GRACE, **kwargs})


@pytest.fixture
def cleanup_storage(monkeypatch, storage: FakeStorage) -> FakeStorage:
    from app.modules.agent_run.infrastructure import expired_artifact_cleanup as module

    monkeypatch.setattr(module, "delete_object", storage.delete)
    return storage


def _keys(index: int) -> tuple[str, str]:
    artifact_id = _edit_artifact(index).id
    return _artifact_object_key(RUN_ID, artifact_id), _preview_object_key(RUN_ID, artifact_id)


def _awaiting(runs, *indexes: int):
    created = tuple(_edit_artifact(index) for index in indexes)
    runs.create_with_planning_job(_workflow_run(), "edit-job", created)
    _plan_for(runs, created)
    return created


def _age(connect, age: str = "25 hours") -> None:
    with connect() as conn:
        conn.execute("UPDATE agent_run_artifacts SET expires_at = now() - %s::interval", (age,))


def _artifact_count(connect) -> int:
    with connect() as conn:
        return conn.execute("SELECT count(*) AS count FROM agent_run_artifacts").fetchone()["count"]


def _run_status(connect) -> str:
    with connect() as conn:
        return conn.execute("SELECT status FROM agent_runs WHERE id = %s", (RUN_ID,)).fetchone()["status"]


def _approve(runs, selected: tuple[str, ...] | None = None):
    run, plan = runs.get_current_plan_for_user("ws", "user", RUN_ID)
    return runs.approve_and_enqueue(run, plan, "approval-1", "execution-job", selected)


def _wait_until(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "조건을 기다리다 시간이 지났습니다."
        time.sleep(0.02)


def test_expired_past_grace_removes_both_objects_and_the_row(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect)

    result = _cleanup()

    assert (result.deleted, result.failed, result.rejected) == (1, 0, 0)
    assert cleanup_storage.objects == {}
    assert set(cleanup_storage.deleted) == set(_keys(1))
    assert _artifact_count(connect) == 0
    assert _run_status(connect) == "awaiting_approval"


def test_artifact_without_preview_or_object_key_is_removed(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    runs.create_with_planning_job(_workflow_run(), "edit-job", (replace(_edit_artifact(1), preview_json=None),))
    _age(connect)
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status = 'failed'")

    assert _cleanup().deleted == 1
    assert cleanup_storage.deleted == [_keys(1)[0]]

    runs.register_artifact(
        run_id=RUN_ID, workspace_id="ws", user_id="user", artifact_id="no-object",
        content_hash=_edit_artifact(1).content_hash, purpose="create_document", document_id=None,
        base_version=None, target=None, markdown=_edit_artifact(1).markdown,
    )
    with connect() as conn:
        conn.execute("UPDATE agent_run_artifacts SET object_key = NULL, expires_at = now() - interval '25 hours'")

    assert _cleanup().deleted == 1
    assert _artifact_count(connect) == 0


def test_within_grace_is_left_alone(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect, "23 hours")

    assert _cleanup().deleted == 0

    assert _artifact_count(connect) == 1
    assert cleanup_storage.deleted == []


def test_not_yet_expired_is_left_alone_even_without_grace(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)

    assert _cleanup(grace_seconds=0).deleted == 0

    assert _artifact_count(connect) == 1


def test_null_expiry_is_never_removed_even_when_old(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    with connect() as conn:
        conn.execute(
            "UPDATE agent_run_artifacts SET expires_at = NULL, created_at = now() - interval '400 days'"
        )
        conn.execute("UPDATE agent_runs SET status = 'cancelled', finished_at = now() - interval '89 days'")

    assert _cleanup(grace_seconds=0).deleted == 0

    assert _artifact_count(connect) == 1
    assert cleanup_storage.deleted == []


def test_undo_artifact_pinned_by_save_undo_request_is_kept(repositories, cleanup_storage) -> None:
    runs, jobs, connect = repositories
    _awaiting(runs, 1)
    _approve(runs)
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_tool_executions (id, run_id, plan_id, operation_id, tool_name, idempotency_key, "
            "attempt, status) VALUES ('undo-1', %s, 'edit-plan', 'op-1', 'apply_document_edit', 'undo:op-1', 0, "
            "'undo_pending')",
            (RUN_ID,),
        )
    runs.register_artifact(
        run_id=RUN_ID, workspace_id="ws", user_id="user", artifact_id="undo-artifact",
        content_hash=_edit_artifact(1).content_hash, purpose="apply_document_edit", document_id="doc-1",
        base_version=2, target=_edit_artifact(1).target, markdown=_edit_artifact(1).markdown,
    )
    jobs.save_undo_request("undo-1", "apply_document_edit", {"content_artifact_id": "undo-artifact"})
    with connect() as conn:
        conn.execute(
            "UPDATE agent_run_artifacts SET expires_at = now() - interval '25 hours' WHERE id <> 'undo-artifact'"
        )
        conn.execute("UPDATE agent_runs SET status = 'cancelled', finished_at = now()")

    assert _cleanup().deleted == 1

    with connect() as conn:
        rows = conn.execute("SELECT id, expires_at FROM agent_run_artifacts").fetchall()
    assert [(row["id"], row["expires_at"]) for row in rows] == [("undo-artifact", None)]
    assert _artifact_object_key(RUN_ID, "undo-artifact") in cleanup_storage.objects


@pytest.mark.parametrize("status", ["queued", "planning", "executing", "verifying", "cancel_requested", "rolling_back"])
def test_artifacts_of_a_run_in_progress_are_left_alone(repositories, cleanup_storage, status: str) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect, "30 days")
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status = %s", (status,))

    assert _cleanup().deleted == 0

    assert _artifact_count(connect) == 1


@pytest.mark.parametrize(
    "status",
    ["awaiting_approval", "clarification_required", "completed", "partial_failed", "failed", "conflicted",
     "rejected", "cancelled", "rollback_failed"],
)
def test_artifacts_of_a_run_waiting_or_finished_are_removed(repositories, cleanup_storage, status: str) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect)
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status = %s", (status,))

    assert _cleanup().deleted == 1


def test_batch_limit_leaves_the_rest_for_the_next_cycle(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1, 2, 3)
    _age(connect)

    assert _cleanup(limit=2).deleted == 2
    assert _artifact_count(connect) == 1
    assert _cleanup(limit=2).deleted == 1
    assert _artifact_count(connect) == 0


def test_key_outside_the_run_scope_is_not_deleted_and_row_stays(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1, 2)
    other_run_key = _artifact_object_key("other-run", "artifact")
    cleanup_storage.objects[other_run_key] = "다른 run의 본문"
    with connect() as conn:
        conn.execute(
            "UPDATE agent_run_artifacts SET preview_object_key = %s WHERE id = %s",
            (other_run_key, _edit_artifact(1).id),
        )
    _age(connect)

    result = _cleanup()

    assert (result.deleted, result.failed, result.rejected) == (1, 0, 1)
    assert other_run_key in cleanup_storage.objects
    # 범위를 벗어난 행은 Markdown 객체도 건드리지 않는다.
    assert _keys(1)[0] in cleanup_storage.objects
    with connect() as conn:
        rows = conn.execute("SELECT id FROM agent_run_artifacts").fetchall()
    assert [row["id"] for row in rows] == [_edit_artifact(1).id]


@pytest.mark.parametrize(
    "key",
    [
        "s3://other-bucket/agent-runs/{scope}/artifacts/x.md",
        "agent-runs/{scope}/../{other}/artifacts/x.md",
        "wiki/ws/page.md",
        "agent-runs/{scope}",
    ],
)
def test_suspicious_keys_are_rejected(repositories, cleanup_storage, key: str) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    scope = _artifact_object_key(RUN_ID, "x").split("/")[1]
    other = _artifact_object_key("other-run", "x").split("/")[1]
    with connect() as conn:
        conn.execute("UPDATE agent_run_artifacts SET object_key = %s", (key.format(scope=scope, other=other),))
    _age(connect)

    result = _cleanup()

    assert (result.deleted, result.rejected) == (0, 1)
    assert cleanup_storage.deleted == []


def test_stored_s3_uri_inside_the_scope_is_accepted(repositories, cleanup_storage, monkeypatch) -> None:
    runs, _, connect = repositories
    monkeypatch.setenv("S3_BUCKET", "fruition-storage")
    _awaiting(runs, 1)
    uri = f"s3://fruition-storage/{_keys(1)[0]}"
    with connect() as conn:
        conn.execute("UPDATE agent_run_artifacts SET object_key = %s", (uri,))
    _age(connect)

    assert _cleanup().deleted == 1
    assert uri in cleanup_storage.deleted


def test_object_failure_keeps_the_row_and_the_next_cycle_retries(repositories, cleanup_storage, caplog) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1, 2)
    _age(connect)
    failing_key = _keys(1)[1]
    cleanup_storage.failing.add(failing_key)

    with caplog.at_level(logging.INFO):
        first = _cleanup()

    assert (first.deleted, first.failed) == (1, 1)
    with connect() as conn:
        rows = conn.execute("SELECT id FROM agent_run_artifacts").fetchall()
    assert [row["id"] for row in rows] == [_edit_artifact(1).id]
    # 오류 원문은 로그에 남기지 않는다.
    assert "요청 실패" not in caplog.text

    cleanup_storage.failing.clear()
    second = _cleanup()

    assert (second.deleted, second.failed) == (1, 0)
    assert cleanup_storage.objects == {} and _artifact_count(connect) == 0


def test_storage_outage_stops_the_cycle_early(repositories, cleanup_storage) -> None:
    from app.modules.agent_run.infrastructure.expired_artifact_cleanup import MAX_STORAGE_FAILURES_PER_CYCLE

    runs, _, connect = repositories
    created = MAX_STORAGE_FAILURES_PER_CYCLE + 2
    _awaiting(runs, *range(1, created + 1))
    _age(connect)
    attempts: list[str] = []

    def outage(key: str) -> None:
        attempts.append(key)
        raise _s3_error("ServiceUnavailable")

    cleanup_storage.on_delete = outage

    result = _cleanup()

    assert (result.deleted, result.failed) == (0, MAX_STORAGE_FAILURES_PER_CYCLE)
    assert len(attempts) == MAX_STORAGE_FAILURES_PER_CYCLE
    assert _artifact_count(connect) == created


def test_missing_object_counts_as_removed(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect)

    def already_gone(key: str) -> None:
        raise _s3_error("NoSuchKey")

    cleanup_storage.on_delete = already_gone

    result = _cleanup()

    assert (result.deleted, result.failed) == (1, 0)
    assert _artifact_count(connect) == 0


def test_two_workers_at_once_delete_each_artifact_once(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    # 두 run을 만들어 run 잠금이 겹치지 않는 상태에서 동시에 돈다.
    runs.create_with_planning_job(_workflow_run(), "edit-job", (_edit_artifact(1),))
    second = replace(_workflow_run(), id="edit-run-2")
    second_artifact = replace(_edit_artifact(2), id="second-artifact")
    runs.create_with_planning_job(second, "edit-job-2", (second_artifact,))
    _age(connect)
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status = 'failed'")
    first_inside = threading.Event()
    second_done = threading.Event()
    results = {}

    def hold_first(key: str) -> None:
        if not first_inside.is_set():
            first_inside.set()
            assert second_done.wait(20)

    cleanup_storage.on_delete = hold_first

    def first_worker() -> None:
        results["first"] = _cleanup(limit=1)

    thread = threading.Thread(target=first_worker)
    thread.start()
    assert first_inside.wait(20)
    # 첫 worker가 잠근 artifact를 기다리지 않고 건너뛰어 나머지를 지운다.
    results["second"] = _cleanup()
    second_done.set()
    thread.join(20)

    assert (results["first"].deleted, results["second"].deleted) == (1, 1)
    assert len(cleanup_storage.deleted) == len(set(cleanup_storage.deleted)) == 4
    assert cleanup_storage.objects == {} and _artifact_count(connect) == 0


def test_cleanup_skips_a_run_locked_by_an_approval_in_progress(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect)

    with connect() as approval:
        approval.execute("SELECT 1 FROM agent_runs WHERE id = %s FOR UPDATE", (RUN_ID,))

        assert _cleanup().deleted == 0

        assert _artifact_count(connect) == 1
        assert cleanup_storage.deleted == []


def test_approval_during_cleanup_waits_then_is_refused(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect)
    deleting = threading.Event()
    finish = threading.Event()

    def hold(key: str) -> None:
        deleting.set()
        assert finish.wait(20)

    cleanup_storage.on_delete = hold
    cleaner = threading.Thread(target=_cleanup)
    cleaner.start()
    assert deleting.wait(20)
    outcome: list[object] = []

    def approve() -> None:
        try:
            outcome.append(_approve(runs))
        except ValueError as exc:
            outcome.append(exc)

    approver = threading.Thread(target=approve)
    approver.start()

    def approval_is_blocked() -> bool:
        with connect() as conn:
            row = conn.execute(
                "SELECT count(*) AS count FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND query LIKE '%FOR UPDATE%' AND query LIKE '%agent_runs%'"
            ).fetchone()
        return row["count"] > 0

    _wait_until(approval_is_blocked)
    finish.set()
    cleaner.join(20)
    approver.join(20)

    assert isinstance(outcome[0], ValueError)
    assert _artifact_count(connect) == 0
    assert _run_status(connect) == "awaiting_approval"


def test_approval_committing_first_keeps_the_artifact_of_the_executing_run(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _approve(runs)
    _age(connect, "30 days")

    assert _cleanup().deleted == 0

    assert _artifact_count(connect) == 1


def test_previews_after_removal_are_empty_with_a_past_expiry(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1, 2)
    _age(connect)
    _cleanup()

    previews = runs.get_edit_previews("ws", "user", RUN_ID)

    assert previews["items"] == []
    # 화면은 expires_at이 지났는지로 수락을 막는다. null이면 수락 버튼이 열린다.
    assert previews["expires_at"] < datetime.now(timezone.utc)


def test_previews_with_some_artifacts_removed_keep_the_rest_and_a_past_expiry(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1, 2)
    _age(connect)
    assert _cleanup(limit=1).deleted == 1

    previews = runs.get_edit_previews("ws", "user", RUN_ID)

    assert len(previews["items"]) == 1
    assert previews["expires_at"] < datetime.now(timezone.utc)


def test_previews_survive_object_gone_while_the_expired_row_remains(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1, 2)
    _age(connect, "2 hours")
    del cleanup_storage.objects[_keys(1)[1]]

    previews = runs.get_edit_previews("ws", "user", RUN_ID)

    assert [item["document_id"] for item in previews["items"]] == ["doc-2"]
    assert previews["expires_at"] < datetime.now(timezone.utc)


def test_previews_still_report_a_missing_object_of_a_live_artifact(repositories, cleanup_storage) -> None:
    runs, _, _ = repositories
    _awaiting(runs, 1)
    del cleanup_storage.objects[_keys(1)[1]]

    with pytest.raises(S3Error):
        runs.get_edit_previews("ws", "user", RUN_ID)


def test_previews_of_a_plan_without_artifacts_stay_null(repositories, cleanup_storage) -> None:
    runs, _, _ = repositories
    runs.create_with_planning_job(_workflow_run(), "edit-job", (_edit_artifact(1),))

    assert runs.get_edit_previews("ws", "user", RUN_ID) == {"items": [], "expires_at": None}


def test_approve_is_refused_after_removal_and_the_run_keeps_waiting(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect)
    _cleanup()

    with pytest.raises(ValueError):
        _approve(runs)

    assert _run_status(connect) == "awaiting_approval"
    with connect() as conn:
        jobs = conn.execute("SELECT count(*) AS count FROM agent_jobs WHERE job_type = 'execution'").fetchone()
        approvals = conn.execute("SELECT count(*) AS count FROM agent_approvals").fetchone()
    assert (jobs["count"], approvals["count"]) == (0, 0)


def test_approve_is_refused_once_expired_even_before_removal(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect, "1 minute")

    with pytest.raises(ValueError):
        _approve(runs)

    assert _run_status(connect) == "awaiting_approval"


def test_approve_ignores_expiry_of_operations_left_out_of_a_partial_approval(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1, 2)
    with connect() as conn:
        conn.execute(
            "UPDATE agent_run_artifacts SET expires_at = now() - interval '1 minute' WHERE id = %s",
            (_edit_artifact(1).id,),
        )

    with pytest.raises(ValueError):
        _approve(runs, ("op-1",))
    approved = _approve(runs, ("op-2",))

    assert approved.status == "executing"


def test_approve_with_live_artifacts_still_works(repositories, cleanup_storage) -> None:
    runs, _, _ = repositories
    _awaiting(runs, 1)

    assert _approve(runs).status == "executing"


def test_revise_is_refused_after_removal_so_planning_cannot_fall_through(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect)
    _cleanup()

    with pytest.raises(ValueError):
        runs.revise("ws", "user", RUN_ID, "다르게 고쳐 줘", "revise-job")

    assert _run_status(connect) == "awaiting_approval"
    with connect() as conn:
        job = conn.execute("SELECT 1 FROM agent_jobs WHERE id = 'revise-job'").fetchone()
    assert job is None


def test_reject_still_closes_a_run_after_removal(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect)
    _cleanup()

    assert runs.reject("ws", "user", RUN_ID, "reject-1").status == "rejected"


def test_routes_after_removal_return_200_with_past_expiry_and_409(repositories, cleanup_storage) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    run, plan = runs.get_current_plan_for_user("ws", "user", RUN_ID)
    _age(connect)
    _cleanup()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_run_repository] = lambda: runs
    app.dependency_overrides[get_approve_agent_plan_use_case] = lambda: ApproveAgentPlanUseCase(runs)
    client = TestClient(app)

    previews = client.get(f"/agent/runs/{RUN_ID}/edit-previews", params={"workspace_id": "ws", "user_id": "user"})
    approved = client.post(
        f"/agent/runs/{RUN_ID}/approve",
        json={"workspace_id": "ws", "user_id": "user", "plan_version": plan.version,
              "operation_hash": plan.operation_hash},
    )

    assert previews.status_code == 200
    assert previews.json()["items"] == []
    assert previews.json()["expires_at"] is not None
    assert approved.status_code == 409


def test_cleanup_logs_counts_without_content(repositories, cleanup_storage, caplog) -> None:
    runs, _, connect = repositories
    _awaiting(runs, 1)
    _age(connect)
    markdown = _edit_artifact(1).markdown

    with caplog.at_level(logging.DEBUG, logger="app.modules.agent_run.infrastructure.expired_artifact_cleanup"):
        _cleanup()

    assert markdown not in caplog.text


class TestSchedule:
    @pytest.fixture
    def worker_module(self):
        import agent_worker

        return agent_worker

    def test_not_due_does_nothing(self, worker_module, monkeypatch) -> None:
        cleanup = MagicMock()
        monkeypatch.setattr(worker_module, "delete_expired_artifacts", cleanup)

        assert worker_module._delete_expired_artifacts_if_due(GRACE, 100.0, 50.0) == 100.0

        cleanup.assert_not_called()

    def test_due_runs_and_waits_a_full_interval(self, worker_module, monkeypatch, caplog) -> None:
        from app.modules.agent_run.infrastructure.expired_artifact_cleanup import ExpiredArtifactCleanup

        cleanup = MagicMock(return_value=ExpiredArtifactCleanup(deleted=3))
        monkeypatch.setattr(worker_module, "delete_expired_artifacts", cleanup)

        with caplog.at_level(logging.INFO):
            next_at = worker_module._delete_expired_artifacts_if_due(GRACE, 0.0, 50.0)

        cleanup.assert_called_once_with(grace_seconds=GRACE)
        assert next_at == 50.0 + worker_module._ARTIFACT_CLEANUP_INTERVAL_SECONDS
        assert "3" in caplog.text

    def test_full_batch_comes_back_soon_to_drain_the_backlog(self, worker_module, monkeypatch) -> None:
        from app.modules.agent_run.infrastructure.expired_artifact_cleanup import (
            ARTIFACT_CLEANUP_BATCH_SIZE,
            ExpiredArtifactCleanup,
        )

        monkeypatch.setattr(
            worker_module,
            "delete_expired_artifacts",
            MagicMock(return_value=ExpiredArtifactCleanup(deleted=ARTIFACT_CLEANUP_BATCH_SIZE)),
        )

        next_at = worker_module._delete_expired_artifacts_if_due(GRACE, 0.0, 50.0)

        assert next_at == 50.0 + worker_module._ARTIFACT_CLEANUP_BACKLOG_INTERVAL_SECONDS

    def test_grace_defaults_to_one_day(self, worker_module, monkeypatch) -> None:
        monkeypatch.delenv("AGENT_ARTIFACT_CLEANUP_GRACE_SECONDS", raising=False)

        assert worker_module._artifact_cleanup_grace_seconds() == 86400

    def test_grace_reads_the_environment(self, worker_module, monkeypatch) -> None:
        monkeypatch.setenv("AGENT_ARTIFACT_CLEANUP_GRACE_SECONDS", "3600")

        assert worker_module._artifact_cleanup_grace_seconds() == 3600

    def test_negative_grace_fails_at_startup(self, worker_module, monkeypatch) -> None:
        monkeypatch.setenv("AGENT_ARTIFACT_CLEANUP_GRACE_SECONDS", "-1")

        with pytest.raises(ValueError):
            worker_module._artifact_cleanup_grace_seconds()
