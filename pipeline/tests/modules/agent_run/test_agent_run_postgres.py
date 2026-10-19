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
