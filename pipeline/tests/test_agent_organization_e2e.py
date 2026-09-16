"""RUN_LIVE_AGENT_E2E=1에서만 실제 모델을 호출한다. 저장소와 Workspace 도구는 가상이다."""

import json
import os
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.core.llm_env import SUPPORTED_LLM_MODELS
from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.entities import AgentTurnRequest
from app.modules.agent.infrastructure.chat_completions_turn_router import build_agent_turn_router
from app.modules.agent_run.application.approve_agent_plan import ApproveAgentPlanUseCase
from app.modules.agent_run.application.start_agent_run import StartAgentRunUseCase
from app.modules.agent_run.domain.entities import AgentRunContext
from app.modules.agent_run.infrastructure.agent_worker import AgentWorker
from app.modules.agent_run.infrastructure.chat_completions_plan_generator import build_plan_generator


@pytest.mark.skipif(os.environ.get("RUN_LIVE_AGENT_E2E") != "1", reason="실제 모델 호출은 명시적으로 실행한다.")
def test_live_organization_reaches_approval_and_groups_documents():
    provider = os.environ.get("AGENT_E2E_PROVIDER", "openai")
    model = SUPPORTED_LLM_MODELS[provider]
    instruction = "현재까지 업로드 한 문서, 알맞은 폴더 이름 생성해서 주제별로 정리해 줘"
    design, inbox = str(uuid4()), str(uuid4())
    items = {
        design: {"id": design, "type": "folder", "name": "디자인", "parent_id": None, "current_version": 1},
        inbox: {"id": inbox, "type": "folder", "name": "미분류", "parent_id": None, "current_version": 1},
    }
    groups = []
    for names in (
        ("React 컴포넌트 설계", "프런트엔드 CSS 스타일 가이드"),
        ("Spring Boot API 설계", "백엔드 PostgreSQL 데이터 모델"),
        ("마케팅 캠페인 기획", "마케팅 광고 성과 분석"),
    ):
        group = []
        for index, name in enumerate(names):
            doc_id = str(uuid4())
            items[doc_id] = {"id": doc_id, "type": "document", "name": name,
                             "parent_id": inbox if index else None, "current_version": 1}
            group.append(doc_id)
        groups.append(group)
    original = deepcopy(items)
    state = SimpleNamespace(run=None, plan=None, results={})
    repository, gateway = MagicMock(), MagicMock()

    def create_run(run, job_id, artifact=None):
        assert artifact is None
        state.run = run
        return run

    def mark_run(run_id, expected, status):
        assert run_id == state.run.id
        if state.run.status not in expected:
            return False
        state.run = replace(state.run, status=status)
        return True

    def save_plan(run_id, plan):
        assert state.run.status == "planning"
        assert plan.status == "awaiting_approval"
        state.plan = plan
        state.run = replace(state.run, status="awaiting_approval", current_plan_id=plan.id)

    def approve(run, plan, **kwargs):
        assert run == state.run and plan == state.plan
        state.plan = replace(plan, status="approved")
        state.run = replace(run, status="executing")
        return state.run

    def mark_operation(operation_id, expected, status, error_code=None):
        operation = next(op for op in state.plan.operations if op.id == operation_id)
        assert operation.status in expected
        state.plan = replace(state.plan, operations=tuple(
            replace(op, status=status, error_code=error_code) if op.id == operation_id else op
            for op in state.plan.operations
        ))
        return True

    def save_execution(**kwargs):
        assert kwargs["status"] == "succeeded"
        state.results[kwargs["operation_id"]] = kwargs["response_metadata"]

    def finish(run_id):
        assert all(op.status == "succeeded" for op in state.plan.operations)
        state.run = replace(state.run, status="completed")

    def read(tool_name, *, arguments, **kwargs):
        if tool_name == "list_agent_run_artifacts":
            return {"items": []}
        assert tool_name in {"list_root_items", "list_folder_children"}
        parent = None if tool_name == "list_root_items" else arguments["folder_id"]
        return {"items": [
            {**item, "has_children": any(child["parent_id"] == item["id"] for child in items.values())}
            for item in items.values() if item["parent_id"] == parent
        ]}

    def execute(tool_name, *, arguments, **kwargs):
        assert state.run.status == "executing" and state.plan.status == "approved"
        assert kwargs["plan_version"] == state.plan.version
        assert kwargs["operation_hash"] == state.plan.operation_hash
        if tool_name == "create_folder":
            item_id = str(uuid4())
            items[item_id] = {"id": item_id, "type": "folder", "name": arguments["name"],
                              "parent_id": arguments["parent_folder_id"], "current_version": 1}
        else:
            assert tool_name in {"move_document", "move_folder", "rename_folder"}
            item_id = arguments["document_id" if tool_name == "move_document" else "folder_id"]
            assert items[item_id]["current_version"] == arguments["base_version"]
            if tool_name == "rename_folder":
                items[item_id]["name"] = arguments["name"]
            else:
                items[item_id]["parent_id"] = arguments["folder_id" if tool_name == "move_document" else "parent_folder_id"]
            items[item_id]["current_version"] += 1
        item = items[item_id]
        return {**item, "folder_id": item["parent_id"], "parent_folder_id": item["parent_id"]}

    repository.create_with_planning_job.side_effect = create_run
    repository.load_context.side_effect = lambda _: AgentRunContext(state.run, None, ())
    repository.mark_run_status.side_effect = mark_run
    repository.save_plan.side_effect = save_plan
    repository.next_plan_version.return_value = 1
    repository.load_current_plan.side_effect = lambda _: state.plan
    repository.get_current_plan_for_user.side_effect = lambda *args: (state.run, state.plan)
    repository.approve_and_enqueue.side_effect = approve
    repository.mark_operation.side_effect = mark_operation
    repository.load_operation_results.side_effect = lambda *args: dict(state.results)
    repository.save_tool_execution.side_effect = save_execution
    repository.finish_run_from_operations.side_effect = finish
    repository.reserve_tool_call.return_value = True
    repository.remaining_tool_calls.return_value = 40
    gateway.read.side_effect, gateway.execute.side_effect = read, execute
    router = build_agent_turn_router(provider=provider, model=model)
    planner = build_plan_generator(provider=provider, model=model)
    router._client.complete_json = MagicMock(wraps=router._client.complete_json)
    planner._client.complete_json = MagicMock(wraps=planner._client.complete_json)
    unused = MagicMock()
    turn = HandleAgentTurnUseCase(
        router=router, query_use_case=unused, markdown_edit_use_case=unused,
        markdown_create_use_case=unused, agent_run_starter=StartAgentRunUseCase(repository),
    ).execute(AgentTurnRequest(message=instruction, workspace_id="e2e-workspace", user_id="e2e-user",
                               provider=provider, model=model))
    assert turn.action in {"folder_organize", "workspace_workflow"}, turn
    assert turn.run_status == "queued"
    print(f"분류 완료: {turn.action}", flush=True)
    worker = AgentWorker(repository, repository, gateway, planner)
    worker._run_job(SimpleNamespace(run_id=state.run.id, job_type="planning"))
    assert state.run.status == "awaiting_approval", state.run
    assert items == original
    gateway.execute.assert_not_called()
    assert planner._client.complete_json.call_count == 1
    approved_operations = [{"tool": op.tool_name, "arguments": op.arguments, "reason": op.reason}
                           for op in state.plan.operations]
    print(json.dumps({"summary": state.plan.summary, "operations": approved_operations}, ensure_ascii=False), flush=True)
    with pytest.raises(ValueError, match="changed"):
        ApproveAgentPlanUseCase(repository).execute(
            workspace_id=state.run.workspace_id, user_id=state.run.user_id, run_id=state.run.id,
            plan_version=state.plan.version, operation_hash="stale",
        )
    gateway.execute.assert_not_called()
    ApproveAgentPlanUseCase(repository).execute(
        workspace_id=state.run.workspace_id, user_id=state.run.user_id, run_id=state.run.id,
        plan_version=state.plan.version, operation_hash=state.plan.operation_hash,
    )
    worker._run_job(SimpleNamespace(run_id=state.run.id, job_type="execution"))
    assert state.run.status == "completed"
    assert planner._client.complete_json.call_count == 1
    destinations = []
    for group in groups:
        parents = {items[doc_id]["parent_id"] for doc_id in group}
        assert len(parents) == 1 and None not in parents, parents
        parent = parents.pop()
        assert items[parent]["type"] == "folder"
        destinations.append(parent)
        for doc_id in group:
            assert items[doc_id]["name"] == original[doc_id]["name"]
    assert len(set(destinations)) == 3
    assert any(op.tool_name == "create_folder" for op in state.plan.operations)
    assert unused.mock_calls == []
    print(json.dumps({"provider": provider, "model": model, "status": state.run.status,
        "router_calls": router._client.complete_json.call_count, "planner_calls": planner._client.complete_json.call_count,
        "executed": gateway.execute.call_count,
        "folders": {items[parent]["name"]: [items[doc]["name"] for doc in group]
                    for parent, group in zip(destinations, groups)}}, ensure_ascii=False, indent=2), flush=True)
