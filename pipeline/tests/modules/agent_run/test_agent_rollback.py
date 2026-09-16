from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.modules.agent_run.application.ports import ToolGatewayError
from app.modules.agent_run.domain.entities import AgentRun, AgentRunContext
from app.modules.agent_run.domain.plan import AgentPlanOperation, build_agent_plan
from app.modules.agent_run.infrastructure.agent_worker import AgentWorker


class Workspace:
    def __init__(self):
        self.items = {
            "design": dict(id="design", type="folder", name="디자인", parent_id=None, current_version=1),
            "inbox": dict(id="inbox", type="folder", name="미분류", parent_id=None, current_version=1),
        }
        for i, name in enumerate(("React 설계", "CSS 가이드", "Spring API", "PostgreSQL 모델", "마케팅 기획", "광고 성과")):
            self.items[f"doc-{i}"] = dict(id=f"doc-{i}", type="document", name=name,
                                         parent_id="inbox" if i % 2 else None, current_version=1)
        self.orders = {None: [key for key, item in self.items.items() if item["parent_id"] is None],
                       "inbox": ["doc-1", "doc-3", "doc-5"], "design": []}
        self.responses = {}
        self.calls = []
        self.after_write = lambda tool, key: None

    def state(self):
        return ({key: {k: v for k, v in value.items() if k != "current_version"}
                 for key, value in self.items.items()}, deepcopy(self.orders))

    def read(self, tool, *, arguments, **kwargs):
        if tool == "get_breadcrumb":
            assert set(arguments) == {"folder_id", "document_id"}
            assert (arguments["folder_id"] is None) != (arguments["document_id"] is None)
        if tool in {"list_root_items", "list_folder_children"}:
            parent = arguments.get("folder_id")
            return {"items": [deepcopy(self.items[key]) for key in self.orders[parent]]}
        target_id = arguments.get("folder_id") or arguments.get("document_id")
        if target_id not in self.items:
            raise ToolGatewayError(404, False)
        item = self.items[target_id]
        if tool == "get_document_metadata":
            return {**item, "display_name": item["name"]}
        assert tool == "get_breadcrumb"
        path = [dict(id=target_id, type=item["type"], name=item["name"])]
        parent = item["parent_id"]
        while parent is not None:
            node = self.items[parent]
            path.insert(0, dict(id=parent, type="folder", name=node["name"]))
            parent = node["parent_id"]
        return {"path": path}

    def execute(self, tool, *, arguments, idempotency_key, **kwargs):
        self.calls.append((tool, deepcopy(arguments), idempotency_key))
        if idempotency_key in self.responses:
            return deepcopy(self.responses[idempotency_key])
        if tool == "create_folder":
            target_id = "new-" + arguments["name"]
            parent = arguments.get("parent_folder_id")
            item = dict(id=target_id, type="folder", name=arguments["name"], parent_id=parent, current_version=1)
            self.items[target_id] = item
            self.orders[target_id] = []
            self.orders[parent].append(target_id)
        else:
            target_id = arguments.get("document_id") or arguments.get("folder_id")
            item = self.items[target_id]
            if item["current_version"] != arguments["base_version"]:
                raise ToolGatewayError(409, False)
            if tool == "delete_folder":
                assert arguments["require_empty"] is True
                if self.orders[target_id]:
                    raise ToolGatewayError(409, False)
                self.orders[item["parent_id"]].remove(target_id)
                del self.orders[target_id]
                del self.items[target_id]
            elif tool in {"move_document", "move_folder"}:
                parent = arguments.get("parent_folder_id") if tool == "move_folder" else arguments.get("folder_id")
                self.orders[item["parent_id"]].remove(target_id)
                position = arguments.get("position")
                self.orders[parent].insert(len(self.orders[parent]) if position is None else position, target_id)
                item["parent_id"] = parent
            elif tool in {"rename_document", "rename_folder"}:
                item["name"] = arguments.get("display_name") or arguments["name"]
            else:
                raise AssertionError(tool)
            item["current_version"] += 1
        result = {**item, "folder_id": item["parent_id"], "parent_folder_id": item["parent_id"]}
        self.responses[idempotency_key] = deepcopy(result)
        self.after_write(tool, idempotency_key)
        return result


class Journal:
    def __init__(self, plan):
        self.run = AgentRun("run", "ws", "user", "folder_organize", None, "executing",
                            "현재까지 업로드 한 문서, 알맞은 폴더 이름 생성해서 주제별로 정리해 줘")
        self.plan = plan
        self.records = {}
        self.results = {}
        self.errors = []

    def execution_lock(self, run_id):
        return nullcontext()

    def load_context(self, run_id):
        return AgentRunContext(self.run, None, ())

    def load_current_plan(self, run_id):
        return self.plan

    def mark_run_status(self, run_id, expected, status):
        if self.run.status not in expected:
            return False
        self.run = replace(self.run, status=status)
        return True

    def mark_operation(self, operation_id, expected, status, error_code=None):
        self.plan = replace(self.plan, operations=tuple(
            replace(op, status=status) if op.id == operation_id else op for op in self.plan.operations))
        return True

    def remaining_tool_calls(self, run_id):
        return 40

    def reserve_tool_call(self, run_id):
        return True

    def get_undo_record(self, run_id, operation_id):
        return self.records.get(operation_id)

    def prepare_undo(self, run_id, plan_id, operation_id, tool_name, arguments, before):
        if self.run.status != "executing":
            return False
        self.records[operation_id] = dict(
            id="undo-" + operation_id, operation_id=operation_id, tool_name=tool_name,
            status="undo_pending", forward_status=None, forward_response=None,
            response_metadata=dict(before=deepcopy(before), arguments=deepcopy(arguments)),
            plan_id=plan_id, plan_version=self.plan.version, operation_hash=self.plan.operation_hash,
        )
        return True

    def save_tool_execution(self, *, operation_id, status, response_metadata, **kwargs):
        self.records[operation_id].update(forward_status=status, forward_response=deepcopy(response_metadata),
                                          forward_attempt=kwargs.get("attempt"), forward_error_code=kwargs.get("error_code"))
        if status == "succeeded":
            self.results[operation_id] = response_metadata

    def load_operation_results(self, run_id, plan_id):
        return self.results

    def load_undo_records(self, run_id):
        return [self.records[op.id] for op in reversed(self.plan.operations) if op.id in self.records]

    def save_undo_request(self, record_id, tool_name, arguments):
        record = next(row for row in self.records.values() if row["id"] == record_id)
        record["status"] = "undo_running"
        record["response_metadata"].update(undo_tool=tool_name, undo_arguments=deepcopy(arguments))

    def complete_undo(self, record_id, response):
        record = next(row for row in self.records.values() if row["id"] == record_id)
        record["status"] = "undo_done"
        record["response_metadata"]["undo_response"] = deepcopy(response)

    def finish_rollback(self, run_id, error_code=None):
        self.errors.append(error_code)
        self.run = replace(self.run, status="rollback_failed" if error_code else "cancelled", error_code=error_code)


def organization():
    operations = []
    for i, name in enumerate(("프런트엔드", "백엔드", "마케팅")):
        operations.append(AgentPlanOperation(f"folder-{i}", i + 1, "create_folder", "folder", None, None,
                                             None, None, {"name": name, "parent_folder_id": None}, "주제별 폴더"))
    for i in range(6):
        folder = f"folder-{i // 2}"
        operations.append(AgentPlanOperation(f"move-{i}", i + 4, "move_document", "document", f"doc-{i}", 1,
                                             "inbox" if i % 2 else None, None,
                                             {"document_id": f"doc-{i}", "folder_id": {"$operation_result": folder, "field": "id"},
                                              "position": None, "base_version": 1}, "문서 분류", (folder,)))
    return replace(build_agent_plan("plan", "run", 1, "주제별로 정리합니다.", tuple(operations)), status="approved")


@pytest.mark.parametrize("cancel_after", range(1, 10))
def test_organization_cancellation_restores_two_folders_and_six_documents(cancel_after):
    workspace, journal = Workspace(), Journal(organization())
    before = workspace.state()
    count = 0

    def cancel(tool, key):
        nonlocal count
        if not key.startswith("agent-undo:"):
            count += 1
            if count == cancel_after:
                journal.run = replace(journal.run, status="cancel_requested")

    workspace.after_write = cancel
    worker = AgentWorker(journal, MagicMock(), workspace, MagicMock())
    worker._execute(SimpleNamespace(run_id="run"))
    assert count == cancel_after
    assert journal.run.status == "cancel_requested"
    worker._rollback.execute("run")
    assert journal.run.status == "cancelled"
    assert workspace.state() == before
    assert all(row["status"] == "undo_done" for row in journal.records.values())


def test_rollback_replays_same_request_after_response_loss():
    workspace, journal = Workspace(), Journal(organization())
    worker = AgentWorker(journal, MagicMock(), workspace, MagicMock())
    operation = journal.plan.operations[0]
    worker._execute_operation(journal.load_context("run"), journal.plan, operation, {})
    journal.run = replace(journal.run, status="cancel_requested")

    def fail_after_delete(tool, key):
        if tool == "delete_folder":
            raise ToolGatewayError(None, True)

    workspace.after_write = fail_after_delete
    worker._rollback.execute("run")
    assert journal.run.status == "rollback_failed"
    journal.run = replace(journal.run, status="cancel_requested")
    # 메모리 worker를 버려도 저장된 요청으로 복구를 재개한다.
    AgentWorker(journal, MagicMock(), workspace, MagicMock())._rollback.execute("run")
    assert journal.run.status == "cancelled"
    assert workspace.calls[-1] == workspace.calls[-2]


def test_unknown_forward_outcome_is_not_reported_as_cancelled():
    journal = Journal(organization())
    journal.prepare_undo("run", "plan", "folder-0", "create_folder", {"name": "프런트엔드"}, {})
    next(iter(journal.records.values()))["forward_status"] = "running"
    journal.run = replace(journal.run, status="cancel_requested")
    gateway = MagicMock()
    AgentWorker(journal, MagicMock(), gateway, MagicMock())._rollback.execute("run")
    assert journal.run.status == "rollback_failed"
    assert journal.run.error_code.startswith("rollback_forward_outcome_unknown")
    gateway.execute.assert_not_called()


def test_concurrent_change_is_preserved_and_blocks_rollback():
    workspace, journal = Workspace(), Journal(organization())
    worker = AgentWorker(journal, MagicMock(), workspace, MagicMock())
    worker._execute_operation(journal.load_context("run"), journal.plan, journal.plan.operations[0], {})
    workspace.items["new-프런트엔드"]["current_version"] += 1
    journal.run = replace(journal.run, status="cancel_requested")
    worker._rollback.execute("run")
    assert journal.run.status == "rollback_failed"
    assert "new-프런트엔드" in workspace.items


def test_cancellation_before_execution_does_not_call_tools():
    journal = Journal(organization())
    journal.run = replace(journal.run, status="cancel_requested")
    gateway = MagicMock()
    worker = AgentWorker(journal, MagicMock(), gateway, MagicMock())
    worker._execute(SimpleNamespace(run_id="run"))
    worker._rollback.execute("run")
    assert journal.run.status == "cancelled"
    gateway.execute.assert_not_called()
    gateway.read.assert_not_called()


def test_multiple_operations_on_same_document_restore_in_reverse_order():
    operations = (
        AgentPlanOperation("move", 1, "move_document", "document", "doc-0", 1, None, "inbox",
                           {"document_id": "doc-0", "folder_id": "inbox", "position": 0, "base_version": 1}, "이동"),
        AgentPlanOperation("rename", 2, "rename_document", "document", "doc-0", 2, None, None,
                           {"document_id": "doc-0", "display_name": "새 이름", "base_version": 2}, "이름 변경", ("move",)),
    )
    plan = replace(build_agent_plan("plan", "run", 1, "이동 후 이름 변경", operations), status="approved")
    workspace, journal = Workspace(), Journal(plan)
    before = workspace.state()
    worker = AgentWorker(journal, MagicMock(), workspace, MagicMock())
    for operation in operations:
        worker._execute_operation(journal.load_context("run"), plan, operation, journal.results)
    journal.run = replace(journal.run, status="cancel_requested")
    worker._rollback.execute("run")
    assert journal.run.status == "cancelled"
    assert workspace.state() == before
    assert [call[0] for call in workspace.calls] == ["move_document", "rename_document", "rename_document", "move_document"]
    assert workspace.calls[-1][1]["base_version"] == 4


def test_missing_backend_delete_tool_leaves_retriable_rollback_failure():
    workspace, journal = Workspace(), Journal(organization())
    worker = AgentWorker(journal, MagicMock(), workspace, MagicMock())
    worker._execute_operation(journal.load_context("run"), journal.plan, journal.plan.operations[0], {})
    journal.run = replace(journal.run, status="cancel_requested")
    execute = workspace.execute

    def unavailable(tool, **kwargs):
        if tool == "delete_folder":
            raise ToolGatewayError(400, False)
        return execute(tool, **kwargs)

    workspace.execute = unavailable
    worker._rollback.execute("run")
    assert journal.run.status == "rollback_failed"
    assert journal.run.error_code == "rollback_tool_http_400"
    assert journal.records["folder-0"]["status"] == "undo_running"


def test_snapshot_is_not_replaced_on_worker_retry():
    workspace, journal = Workspace(), Journal(organization())
    worker = AgentWorker(journal, MagicMock(), workspace, MagicMock())
    operation = journal.plan.operations[3]
    arguments = {"document_id": "doc-0", "folder_id": "inbox", "base_version": 1}
    assert worker._rollback.prepare(journal.load_context("run"), journal.plan, operation, arguments)
    snapshot = deepcopy(journal.records[operation.id]["response_metadata"])
    workspace.items["doc-0"]["name"] = "후속 변경"
    assert worker._rollback.prepare(journal.load_context("run"), journal.plan, operation, arguments)
    assert journal.records[operation.id]["response_metadata"] == snapshot


def test_postgres_cancel_job_and_compensation_authorization(monkeypatch):
    """TEST_AGENT_DATABASE_URL을 지정하면 격리 스키마에서 실제 저장소를 검증한다."""
    import os
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository
    from app.modules.agent_run.infrastructure.postgres_agent_run_repository import PostgresAgentRunRepository
    from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    schema = "test_agent_undo_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        for table in ("agent_runs", "agent_plans", "agent_plan_operations", "agent_approvals", "agent_jobs",
                      "agent_tool_executions", "skill_versions"):
            conn.execute(sql.SQL("CREATE TABLE {}.{} (LIKE public.{} INCLUDING ALL)").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-csearch_path={schema}")

    try:
        monkeypatch.setattr(database, "connect_ai", connect)
        runs, jobs, workspace = PostgresAgentRunRepository(), PostgresAgentJobRepository(), Workspace()
        before = workspace.state()
        plan = organization()
        run = runs.create_with_planning_job(replace(Journal(plan).run, status="queued"), "planning-job")
        runs.save_plan(run.id, replace(plan, status="awaiting_approval"))
        run, proposed = runs.get_current_plan_for_user("ws", "user", "run")
        runs.approve_and_enqueue(run, proposed, "approval", "execution-job")
        with connect() as conn:
            conn.execute("UPDATE agent_jobs SET status = 'completed' WHERE id = 'planning-job'")
        execute = workspace.execute

        def authorized_execute(tool, **kwargs):
            authorization = {key: value for key, value in kwargs.items() if key != "idempotency_key"}
            assert runs.authorize_tool_execute(tool_name=tool, **authorization)
            assert not runs.authorize_tool_execute(tool_name=tool, **{**authorization, "user_id": "other-user"})
            assert not runs.authorize_tool_execute(tool_name=tool, **{**authorization, "workspace_id": "other-ws"})
            tampered = {**authorization, "arguments": {**kwargs["arguments"], "base_version": True}}
            assert not runs.authorize_tool_execute(tool_name=tool, **tampered)
            return execute(tool, **kwargs)

        workspace.execute = authorized_execute
        count = 0

        def cancel_during_call(tool, key):
            nonlocal count
            if not key.startswith("agent-undo:"):
                count += 1
                if count == 9:
                    assert runs.cancel("ws", "user", "run").status == "cancel_requested"
                    # 반복 취소는 새 복구 job을 만들지 않는다.
                    assert runs.cancel("ws", "user", "run").status == "cancel_requested"
                    # 기존 실행 job이 결과를 기록하기 전에는 복구 worker가 선점하지 못한다.
                    assert jobs.claim_next("other-worker") is None

        workspace.after_write = cancel_during_call
        worker = AgentWorker(jobs, runs, workspace, MagicMock())
        job = jobs.claim_next("test-worker")
        assert job.job_type == "execution"
        worker.process(job)
        assert runs.get_for_user("ws", "user", "run").status == "cancel_requested"
        job = jobs.claim_next("test-worker")
        assert job.job_type == "rollback"
        worker.process(job)
        final = runs.get_for_user("ws", "user", "run")
        assert final.status == "cancelled", final.error_code
        assert final.finished_at is not None
        assert workspace.state() == before
        assert jobs.claim_next("test-worker") is None
        assert all(row["status"] == "undo_done" for row in jobs.load_undo_records("run"))
        assert runs.cancel("ws", "user", "run").status == "cancelled"
        # 취소된 상위 턴의 Kafka 재전달과 늦은 모델 응답도 run을 되살리지 않는다.
        from app.core.pipeline_control import PipelineRunCancelledError
        from app.workers import task_worker
        command = dict(run_id="turn", workspace_id="ws", user_id="user", message="문서를 정리해 줘",
                       provider="openai", model="gpt-5-nano")
        with connect() as conn:
            conn.execute(
                "INSERT INTO agent_runs (id, workspace_id, user_id, action, status, request_summary, command_envelope_hash) "
                "VALUES ('turn', 'ws', 'user', 'markdown_turn', 'executing', '요청', %s)",
                (task_worker._agent_command_hash(command),),
            )
            conn.execute("INSERT INTO agent_jobs (id, run_id, job_type, status) "
                         "VALUES ('turn-job', 'turn', 'markdown_turn', 'executing')")
        use_case, response = MagicMock(), MagicMock()
        use_case.execute.side_effect = lambda request: runs.cancel("ws", "user", "turn")
        response.model_dump.return_value = {"action": "conversation_reply", "message": "늦은 응답"}
        with monkeypatch.context() as scoped:
            scoped.setattr(task_worker, "_register_agent_command", lambda command: ("execute", None))
            scoped.setattr(task_worker, "build_handle_agent_turn_use_case", lambda **kwargs: use_case)
            scoped.setattr(task_worker, "agent_to_response", lambda value: response)
            with pytest.raises(PipelineRunCancelledError):
                task_worker._handle_agent(command)
        assert runs.get_for_user("ws", "user", "turn").status == "cancel_requested"
        with pytest.raises(PipelineRunCancelledError):
            task_worker._register_agent_command(command)
        assert task_worker._failure_is_durable({**command, "kind": "agent"})
        worker.process(jobs.claim_next("test-worker"))
        assert runs.get_for_user("ws", "user", "turn").status == "cancelled"
        # 부모 참조는 생성과 함께 저장되며 같은 턴 재실행은 자식을 중복 생성하지 않는다.
        with connect() as conn:
            conn.execute("INSERT INTO agent_runs (id, workspace_id, user_id, action, status, request_summary) "
                         "VALUES ('parent', 'ws', 'user', 'markdown_turn', 'executing', '이름 변경')")
        child_runs = PostgresAgentRunRepository(parent_run_id="parent")
        child = child_runs.create_with_planning_job(replace(run, id="child"), "child-planning")
        replay = child_runs.create_with_planning_job(replace(run, id="duplicate"), "duplicate-job")
        assert replay.id == child.id
        op = AgentPlanOperation("child-rename", 1, "rename_document", "document", "doc-0",
                                workspace.items["doc-0"]["current_version"], None, None,
                                {"document_id": "doc-0", "display_name": "변경된 제목",
                                 "base_version": workspace.items["doc-0"]["current_version"]}, "제목 변경")
        child_plan = build_agent_plan("child-plan", "child", 1, "제목 변경", (op,))
        runs.save_plan(child.id, child_plan)
        child, child_plan = runs.get_current_plan_for_user("ws", "user", child.id)
        runs.approve_and_enqueue(child, child_plan, "child-approval", "child-execution")
        child_plan = jobs.load_current_plan(child.id)
        worker._execute_operation(jobs.load_context(child.id), child_plan, child_plan.operations[0], {})
        assert workspace.items["doc-0"]["name"] == "변경된 제목"
        # 부모가 진행 중인 동안 먼저 끝난 자식의 변경도 부모 취소에 포함된다.
        with connect() as conn:
            conn.execute("UPDATE agent_jobs SET status = 'completed' WHERE run_id = 'child'")
            conn.execute("UPDATE agent_runs SET status = 'completed' WHERE id = 'child'")
        # 자식 생성 뒤 상위 응답 처리에서 실패해도 복구할 자식 참조를 잃지 않는다.
        failed_use_case = MagicMock()
        failed_use_case.execute.side_effect = ValueError("응답 처리 실패")
        with monkeypatch.context() as scoped:
            scoped.setattr(task_worker, "_register_agent_command", lambda command: ("execute", None))
            scoped.setattr(task_worker, "build_handle_agent_turn_use_case", lambda **kwargs: failed_use_case)
            with pytest.raises(ValueError, match="응답 처리 실패"):
                task_worker._handle_agent({**command, "run_id": "parent"})
        assert runs.get_for_user("ws", "user", "parent").status == "failed"
        assert runs.cancel("ws", "user", "parent").status == "cancel_requested"
        with pytest.raises(PipelineRunCancelledError):
            child_runs.create_with_planning_job(replace(run, id="late-child"), "late-job")
        child_job = jobs.claim_next("test-worker")
        assert child_job.run_id == "child" and child_job.job_type == "rollback"
        assert jobs.claim_next("other-worker") is None
        worker.process(child_job)
        assert workspace.state() == before
        parent_job = jobs.claim_next("test-worker")
        assert parent_job.run_id == "parent" and parent_job.job_type == "rollback"
        worker.process(parent_job)
        assert runs.get_for_user("ws", "user", "parent").status == "cancelled"
        assert jobs.claim_next("test-worker") is None
        # 재시작 뒤 attempt=1로 호출돼도 이전 전송 기록을 지우면 안 된다.
        execution = dict(run_id="run", plan_id="plan", operation_id="folder-0", tool_name="create_folder",
                         idempotency_key="repeat-attempt", attempt=1, response_metadata={})
        for _ in range(2):
            jobs.save_tool_execution(**execution, status="running", error_code=None)
        with connect() as conn:
            running = conn.execute("SELECT attempt, finished_at FROM agent_tool_executions WHERE idempotency_key = 'repeat-attempt'").fetchone()
            assert running == dict(attempt=2, finished_at=None)
        jobs.save_tool_execution(**execution, status="failed", error_code="tool_http_409")
        with connect() as conn:
            assert conn.execute("SELECT attempt FROM agent_tool_executions WHERE idempotency_key = 'repeat-attempt'").fetchone()["attempt"] == 2
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_document_edit_rollback_registers_original_content_and_verifies_hash():
    import hashlib
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    original, changed = "# 원래 문서\n\n원래 본문", "# 바뀐 문서\n\n바뀐 본문"
    operation = AgentPlanOperation(
        "edit", 1, "apply_document_edit", "document", "doc-0", 3, None, None,
        {"document_id": "doc-0", "base_version": 3, "content_artifact_id": "new-content",
         "content_hash": "sha256:" + digest(changed), "target": {"type": "whole_document", "start_line": 1, "end_line": 3}}, "문서 편집",
    )
    plan = replace(build_agent_plan("plan", "run", 1, "편집", (operation,)), status="approved")
    journal, artifacts, gateway = Journal(plan), MagicMock(), MagicMock()
    current = {"id": "doc-0", "markdown": original, "content_hash": digest(original), "edit_revision": 3}
    gateway.read.side_effect = lambda *args, **kwargs: deepcopy(current)

    def execute(tool, *, arguments, idempotency_key, **kwargs):
        assert tool == "apply_document_edit"
        assert arguments["base_version"] == current["edit_revision"]
        markdown = artifacts.register_artifact.call_args.kwargs["markdown"] if idempotency_key.startswith("agent-undo:") else changed
        assert arguments["content_hash"] == "sha256:" + digest(markdown)
        current.update(markdown=markdown, content_hash=digest(markdown), edit_revision=current["edit_revision"] + 1)
        return {"document_id": "doc-0", "current_version": current["edit_revision"], "content_hash": current["content_hash"]}

    gateway.execute.side_effect = execute
    worker = AgentWorker(journal, artifacts, gateway, MagicMock())
    worker._execute_operation(journal.load_context("run"), plan, operation, {})
    journal.run = replace(journal.run, status="cancel_requested")
    worker._rollback.execute("run")
    assert current["markdown"] == original
    assert journal.run.status == "cancelled"
    assert artifacts.register_artifact.call_args.kwargs["base_version"] == 4
    assert artifacts.register_artifact.call_args.kwargs["content_hash"] == "sha256:" + digest(original)
    assert current["edit_revision"] == 5


def test_created_folder_with_unrelated_child_is_not_deleted():
    workspace, journal = Workspace(), Journal(organization())
    worker = AgentWorker(journal, MagicMock(), workspace, MagicMock())
    worker._execute_operation(journal.load_context("run"), journal.plan, journal.plan.operations[0], {})
    workspace.orders["new-프런트엔드"].append("other-users-document")
    journal.run = replace(journal.run, status="cancel_requested")
    worker._rollback.execute("run")
    assert journal.run.status == "rollback_failed"
    assert workspace.orders["new-프런트엔드"] == ["other-users-document"]


@pytest.mark.parametrize("forward_status,error_code", [(None, None), ("failed", "tool_http_409")])
def test_unstarted_or_definitively_rejected_step_still_restores_previous_steps(forward_status, error_code):
    workspace, journal = Workspace(), Journal(organization())
    before = workspace.state()
    worker = AgentWorker(journal, MagicMock(), workspace, MagicMock())
    worker._execute_operation(journal.load_context("run"), journal.plan, journal.plan.operations[0], {})
    journal.prepare_undo("run", "plan", "folder-1", "create_folder", {"name": "백엔드"}, {})
    journal.records["folder-1"].update(forward_status=forward_status, forward_attempt=1, forward_error_code=error_code)
    journal.run = replace(journal.run, status="cancel_requested")
    worker._rollback.execute("run")
    assert journal.run.status == "cancelled"
    assert workspace.state() == before


@pytest.mark.parametrize("crash", [False, True], ids=["before-delivery", "worker-killed"])
def test_postgres_turn_cancellation_through_worker_main(monkeypatch, crash):
    """실제 진입점에서 모델 없는 취소와 실행 프로세스 종료 후 복구를 검증한다."""
    import os
    import select
    import json
    import subprocess
    import sys
    import time
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from uuid import uuid4

    import agent_worker
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from app.modules.agent_run.infrastructure.postgres_agent_job_repository import PostgresAgentJobRepository
    from app.modules.agent_run.infrastructure.postgres_agent_run_repository import (
        PostgresAgentRunRepository, register_cancelled_turn,
    )
    from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("격리 PostgreSQL 테스트 URL이 지정되지 않았습니다.")
    schema = "test_turn_cancel_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-csearch_path={schema}")

    process = None
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with connect() as conn:
            conn.execute((Path(__file__).parents[3] / "db/ai_schema.sql").read_text())
        monkeypatch.setattr(database, "connect_ai", connect)
        command = dict(kind="agent", run_id="turn", workspace_id="ws", user_id="user",
                       message="현재까지 업로드 한 문서, 알맞은 폴더 이름 생성해서 주제별로 정리해 줘",
                       provider="openai", model="gpt-5-nano")
        runs, jobs = PostgresAgentRunRepository(), PostgresAgentJobRepository()
        if crash:
            # 별도 프로세스에서 실제 Kafka 처리 진입점의 잠금을 획득한다.
            process = subprocess.Popen([sys.executable, "-u", "-c", '''
import json, os, time
import psycopg
from psycopg.rows import dict_row
from app.workers import task_worker
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

def connect():
    return psycopg.connect(os.environ["TEST_AGENT_DATABASE_URL"], row_factory=dict_row,
                           options="-csearch_path=" + os.environ["TEST_TURN_SCHEMA"])
database.connect_ai = connect

def handle(command, publisher):
    task_worker._register_agent_command(command)
    print("executing", flush=True)
    time.sleep(60)
task_worker._handle = handle
task_worker._handle_controlled(json.loads(os.environ["TEST_TURN_COMMAND"]))
'''], env={**os.environ, "TEST_TURN_SCHEMA": schema, "TEST_TURN_COMMAND": json.dumps(command)},
                stdout=subprocess.PIPE, text=True, cwd=Path(__file__).parents[3])
            assert select.select([process.stdout], [], [], 15)[0], "턴 시작 시간 초과"
            assert process.stdout.readline().strip() == "executing"
        else:
            register_cancelled_turn(command)
            context = jobs.load_context("turn")
            assert context.run.provider is None and context.run.model is None
        runs.cancel("ws", "user", "turn")
        job = jobs.claim_next("test-worker")
        assert job is not None and job.job_type == "rollback"
        monkeypatch.setattr(jobs, "claim_next", MagicMock(side_effect=[job, KeyboardInterrupt]))
        monkeypatch.setattr(agent_worker, "PostgresAgentJobRepository", lambda: jobs)
        monkeypatch.setattr(database, "verify_schema", lambda: None)
        monkeypatch.setattr(database, "verify_agent_schema", lambda: None)
        monkeypatch.setattr(agent_worker, "PostgresSaver", lambda *args, **kwargs: None)
        monkeypatch.setattr(agent_worker, "_cleanup_expired_runs_if_due", lambda *args: 0)
        gateway = MagicMock()
        builder = MagicMock(side_effect=AssertionError("복구가 모델을 생성하면 안 됩니다."))
        monkeypatch.setattr(agent_worker, "build_backend_tool_gateway", lambda: gateway)
        monkeypatch.setattr(agent_worker, "build_plan_generator", builder)
        future = executor.submit(agent_worker.main)
        if crash:
            # 실제 잠금 대기를 확인한 뒤 SIGKILL로 연결과 실행 잠금을 해제한다.
            deadline = time.monotonic() + 10
            waiting = False
            while time.monotonic() < deadline and not future.done():
                with connect() as conn:
                    waiting = conn.execute(
                        "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                        "AND objid = (hashtextextended('agent:turn', 0) & 4294967295)::oid"
                    ).fetchone() is not None
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting, "살아 있는 턴과 복구가 실행 잠금을 공유해야 합니다."
            assert runs.get_for_user("ws", "user", "turn").status == "cancel_requested"
            process.kill()
            process.wait(timeout=5)
        with pytest.raises(KeyboardInterrupt):
            future.result(timeout=10)
        final = runs.get_for_user("ws", "user", "turn")
        assert final.status == "cancelled", final.error_code
        builder.assert_not_called()
        gateway.execute.assert_not_called()
        with connect() as conn:
            rollback = conn.execute("SELECT status, attempt_count FROM agent_jobs WHERE id = %s", (job.id,)).fetchone()
            assert rollback == {"status": "completed", "attempt_count": 1}
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
        executor.shutdown(wait=True)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
