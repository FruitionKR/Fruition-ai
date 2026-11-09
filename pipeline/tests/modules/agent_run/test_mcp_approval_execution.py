from dataclasses import replace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.modules.agent_run.application.mcp_approval import (
    ApproveMcpAgentPlanUseCase,
    McpApprovalError,
)
from app.modules.agent_run.application.ports import ToolGatewayError
from app.modules.agent_run.domain.entities import (
    AgentRun,
    AgentRunContext,
    McpApprovalCommand,
    McpApprovalStatus,
    McpExecutionAttempt,
    McpExecutionIdentity,
)
from app.modules.agent_run.domain.plan import AgentPlanOperation, build_agent_plan
from app.modules.agent_run.infrastructure.agent_worker import AgentWorker
from app.modules.agent_run.interfaces.http.schemas import ApproveAgentPlanRequest, McpAgentApprovalRequest


def _approval_fixture():
    operation = AgentPlanOperation(
        id="op-1",
        sequence=1,
        tool_name="apply_document_edit",
        target_type="document",
        target_id="doc_1",
        base_version=1,
        source_parent_id=None,
        destination_parent_id=None,
        arguments={
            "document_id": "doc_1",
            "base_version": 1,
            "target": {"type": "whole_document", "start_line": 1, "end_line": 1},
            "content_artifact_id": "artifact-1",
            "content_hash": "sha256:" + "a" * 64,
        },
        reason="Fix title",
    )
    plan = build_agent_plan("plan-1", "run-1", 1, "Fix title", (operation,))
    plan = replace(plan, status="awaiting_approval")
    run = AgentRun(
        id="run-1",
        workspace_id="workspace-1",
        user_id="user-1",
        action="workspace_workflow",
        skill_version_id=None,
        status="awaiting_approval",
        request_summary="Fix title",
        current_plan_id=plan.id,
        origin="MCP_CLIENT",
        grant_id="00000000-0000-4000-8000-000000000001",
        client_id="https://client.example/metadata",
        policy_generation=2,
    )
    command = McpApprovalCommand(
        run_id=run.id,
        workspace_id=run.workspace_id,
        user_id=run.user_id,
        plan_version=plan.version,
        operation_hash=plan.operation_hash,
        operation_ids=(operation.id,),
        approval_id="00000000-0000-4000-8000-000000000002",
        approval_mode="EXPLICIT_WEB",
        grant_id=run.grant_id,
        client_id=run.client_id,
        policy_generation=run.policy_generation,
    )
    return run, plan, operation, command


def _validity(status="VALID", reason=None, lifecycle="RESERVED"):
    return McpApprovalStatus(
        status=status,
        reason=reason,
        lifecycle_status=lifecycle,
        forward_status="PENDING",
        settlement_status="RESERVED",
    )


def test_mcp_approval_requires_document_reservation_and_keeps_document_approval_id() -> None:
    run, plan, _, command = _approval_fixture()
    repository, gateway = MagicMock(), MagicMock()
    repository.find_mcp_approval.return_value = None
    repository.get_current_plan_for_user.return_value = (run, plan)
    repository.approve_mcp_and_enqueue.return_value = replace(run, status="executing")
    gateway.lookup_mcp_approval.return_value = _validity()

    result = ApproveMcpAgentPlanUseCase(repository, gateway).execute(command)

    assert result.status == "executing"
    assert repository.approve_mcp_and_enqueue.call_args.args[0].approval_id == command.approval_id
    assert repository.approve_mcp_and_enqueue.call_args.args[0].operation_ids == ("op-1",)


def test_mcp_approval_retry_returns_existing_result_without_rechecking_document() -> None:
    run, plan, _, command = _approval_fixture()
    repository, gateway = MagicMock(), MagicMock()
    repository.get_current_plan_for_user.return_value = (replace(run, status="executing"), replace(plan, status="approved"))
    repository.find_mcp_approval.return_value = replace(run, status="executing")

    result = ApproveMcpAgentPlanUseCase(repository, gateway).execute(command)

    assert result.status == "executing"
    gateway.lookup_mcp_approval.assert_not_called()
    repository.approve_mcp_and_enqueue.assert_not_called()


@pytest.mark.parametrize(
    ("status", "reason", "expected_status", "halts"),
    (
        ("UNKNOWN", "RESERVATION_UNKNOWN", 503, False),
        ("FENCED", "USER_CANCEL", 409, False),
        ("FENCED", "CLIENT_DISABLED", 409, True),
    ),
)
def test_mcp_approval_fails_closed_and_only_admin_fences_halt(
    status: str, reason: str, expected_status: int, halts: bool
) -> None:
    run, plan, _, command = _approval_fixture()
    repository, gateway = MagicMock(), MagicMock()
    repository.find_mcp_approval.return_value = None
    repository.get_current_plan_for_user.return_value = (run, plan)
    gateway.lookup_mcp_approval.return_value = _validity(status, reason, None)

    with pytest.raises(McpApprovalError) as raised:
        ApproveMcpAgentPlanUseCase(repository, gateway).execute(command)

    assert raised.value.status_code == expected_status
    assert repository.halt_mcp_run.called is halts
    repository.approve_mcp_and_enqueue.assert_not_called()


def test_mcp_approval_transport_failure_is_retryable_without_enqueue() -> None:
    run, plan, _, command = _approval_fixture()
    repository, gateway = MagicMock(), MagicMock()
    repository.find_mcp_approval.return_value = None
    repository.get_current_plan_for_user.return_value = (run, plan)
    gateway.lookup_mcp_approval.side_effect = ToolGatewayError(None, True)

    with pytest.raises(McpApprovalError) as raised:
        ApproveMcpAgentPlanUseCase(repository, gateway).execute(command)

    assert (raised.value.status_code, raised.value.code) == (503, "DEPENDENCY_UNAVAILABLE")
    repository.approve_mcp_and_enqueue.assert_not_called()


def test_mcp_approval_schema_is_exact_and_strict() -> None:
    _, _, _, command = _approval_fixture()
    value = {
        "schema_version": command.schema_version,
        "workspace_id": command.workspace_id,
        "user_id": command.user_id,
        "plan_version": command.plan_version,
        "operation_hash": command.operation_hash,
        "operation_ids": list(command.operation_ids),
        "approval_id": command.approval_id,
        "approval_mode": command.approval_mode,
        "grant_id": command.grant_id,
        "client_id": command.client_id,
        "policy_generation": command.policy_generation,
    }
    assert McpAgentApprovalRequest.model_validate(value).to_application(command.run_id) == command
    for mutation in ({"price": 100}, {"plan_version": True}, {"approval_id": str(uuid4()).upper()}):
        with pytest.raises(ValidationError):
            McpAgentApprovalRequest.model_validate({**value, **mutation})
        with pytest.raises(ValidationError):
            TypeAdapter(McpAgentApprovalRequest | ApproveAgentPlanRequest).validate_python(
                {**value, **mutation}
            )


def test_mcp_approval_accepts_plan_ordered_subset_and_requires_dependencies() -> None:
    run, plan, first, command = _approval_fixture()
    second = replace(first, id="op-2", sequence=2, depends_on=(first.id,))
    plan = replace(build_agent_plan(plan.id, run.id, 1, "Two", (first, second)), status="awaiting_approval")
    repository, gateway = MagicMock(), MagicMock()
    repository.get_current_plan_for_user.return_value = (run, plan)
    repository.find_mcp_approval.return_value = None
    repository.approve_mcp_and_enqueue.return_value = replace(run, status="executing")
    gateway.lookup_mcp_approval.return_value = _validity()

    with pytest.raises(McpApprovalError):
        ApproveMcpAgentPlanUseCase(repository, gateway).execute(
            replace(command, operation_hash=plan.operation_hash, operation_ids=(second.id,))
        )
    ApproveMcpAgentPlanUseCase(repository, gateway).execute(
        replace(command, operation_hash=plan.operation_hash, operation_ids=(second.id, first.id))
    )
    assert repository.approve_mcp_and_enqueue.call_args.args[0].operation_ids == (first.id, second.id)


def _receipt(command: McpApprovalCommand, attempt_id: str, outcome="COMMITTED"):
    no_change = outcome == "NO_CHANGE"
    return {
        "receipt_id": "receipt-1",
        "approval_id": command.approval_id,
        "operation_id": "op-1",
        "attempt_id": attempt_id,
        "workspace_id": command.workspace_id,
        "user_id": command.user_id,
        "grant_id": command.grant_id,
        "client_id": command.client_id,
        "tool_name": "apply_document_edit",
        "outcome": outcome,
        "changed": not no_change,
        "resource_type": "DOCUMENT",
        "resource_id": "doc_1",
        "before_version": 1,
        "after_version": 1 if no_change else 2,
        "committed_at": "2026-11-08T00:00:00Z",
    }


def _execution_fixture(created=True):
    run, plan, operation, command = _approval_fixture()
    context = AgentRunContext(replace(run, status="executing"), None, ())
    identity = McpExecutionIdentity(
        command.approval_id, command.grant_id, command.client_id, command.policy_generation
    )
    attempt = McpExecutionAttempt("00000000-0000-4000-8000-000000000003", 1, "started", created)
    repository, gateway = MagicMock(), MagicMock()
    repository.mark_operation.return_value = True
    repository.reserve_tool_call.return_value = True
    repository.load_context.return_value = context
    repository.load_mcp_execution_identity.return_value = identity
    repository.get_or_create_mcp_attempt.return_value = attempt
    worker = AgentWorker(repository, MagicMock(), gateway, MagicMock())
    worker._rollback.prepare = MagicMock(return_value=True)
    worker._rollback.execute = MagicMock()
    return worker, repository, gateway, context, replace(plan, status="approved"), operation, command, attempt


def test_mcp_execute_uses_persisted_identity_and_accepts_only_bound_receipt() -> None:
    worker, repository, gateway, context, plan, operation, command, attempt = _execution_fixture()
    gateway.execute.return_value = _receipt(command, attempt.attempt_id)

    result = worker._execute_operation(context, plan, operation, {})

    assert result == {**gateway.execute.return_value, "document_id": "doc_1", "current_version": 2}
    sent = gateway.execute.call_args.kwargs
    assert (sent["approval_id"], sent["attempt_id"], sent["grant_id"], sent["client_id"], sent["policy_generation"]) == (
        command.approval_id, attempt.attempt_id, command.grant_id, command.client_id, command.policy_generation
    )
    repository.finish_mcp_attempt.assert_called_once_with(
        attempt.attempt_id, "committed", gateway.execute.return_value, None
    )


def test_mcp_restart_reconciles_receipt_without_reexecuting() -> None:
    worker, repository, gateway, context, plan, operation, command, attempt = _execution_fixture(False)
    gateway.lookup_mcp_receipt.return_value = {
        "status": "NO_CHANGE",
        "receipt": _receipt(command, attempt.attempt_id, "NO_CHANGE"),
    }

    result = worker._execute_operation(context, plan, operation, {})

    assert result["outcome"] == "NO_CHANGE"
    gateway.execute.assert_not_called()
    repository.finish_mcp_attempt.assert_called_once_with(
        attempt.attempt_id, "no_change", gateway.lookup_mcp_receipt.return_value["receipt"], None
    )


def test_mcp_receipt_confirmed_create_reuse_points_to_existing_without_execute() -> None:
    worker, repository, gateway, context, plan, operation, _, _ = _execution_fixture()
    operation = replace(
        operation, tool_name="create_document", target_id=None, base_version=None,
        arguments={
            "display_name": "Same", "folder_id": None,
            "content_artifact_id": "artifact", "content_hash": "sha256:" + "a" * 64,
        },
    )
    plan = replace(build_agent_plan(plan.id, plan.run_id, 1, "Create", (operation,)), status="approved")
    repository.get_or_create_mcp_attempt.return_value = McpExecutionAttempt(
        "old-attempt", 0, "existing", False,
        {"receipt_id": "old-receipt", "resource_id": "doc_existing", "after_version": 2},
    )

    result = worker._execute_operation(context, plan, operation, {})

    assert result == {
        "existing_result": True, "id": "doc_existing", "current_version": 2,
        "source_receipt_id": "old-receipt",
    }
    gateway.execute.assert_not_called()
    repository.mark_operation.assert_any_call(operation.id, ("running",), "succeeded")


def test_mcp_receipt_lookup_status_must_match_receipt_outcome() -> None:
    worker, repository, gateway, context, plan, operation, command, attempt = _execution_fixture(False)
    gateway.lookup_mcp_receipt.return_value = {
        "status": "COMMITTED",
        "receipt": _receipt(command, attempt.attempt_id, "NO_CHANGE"),
    }

    assert worker._execute_operation(context, plan, operation, {}) is None

    gateway.execute.assert_not_called()
    repository.finish_mcp_attempt.assert_called_once_with(
        attempt.attempt_id, "unknown", None, "mcp_execution_unknown"
    )


def test_mcp_unknown_response_is_not_replayed_or_rolled_back() -> None:
    worker, repository, gateway, context, plan, operation, _, attempt = _execution_fixture()
    gateway.execute.side_effect = ToolGatewayError(503, True)
    gateway.lookup_mcp_receipt.return_value = {"status": "UNKNOWN", "receipt": None}
    gateway.lookup_mcp_approval.side_effect = ToolGatewayError(None, True)

    assert worker._execute_operation(context, plan, operation, {}) is None

    assert gateway.execute.call_count == 1
    repository.finish_mcp_attempt.assert_called_once_with(
        attempt.attempt_id, "unknown", None, "mcp_execution_unknown"
    )
    worker._rollback.execute.assert_not_called()


def test_mcp_admin_fence_halts_without_rollback() -> None:
    worker, repository, gateway, context, plan, operation, _, attempt = _execution_fixture()
    gateway.execute.side_effect = ToolGatewayError(403, False, "CLIENT_DISABLED")
    gateway.lookup_mcp_receipt.return_value = {"status": "NOT_FOUND", "receipt": None}
    gateway.lookup_mcp_approval.return_value = _validity("FENCED", "CLIENT_DISABLED", None)

    assert worker._execute_operation(context, plan, operation, {}) is None

    repository.halt_mcp_run.assert_called_once_with("run-1", "CLIENT_DISABLED")
    repository.finish_mcp_attempt.assert_called_once_with(
        attempt.attempt_id, "failed", None, "CLIENT_DISABLED"
    )
    worker._rollback.execute.assert_not_called()


def test_mcp_current_permission_failure_does_not_become_admin_halt() -> None:
    worker, repository, gateway, context, plan, operation, _, attempt = _execution_fixture()
    gateway.execute.side_effect = ToolGatewayError(403, False, "EDIT_PERMISSION_DENIED")
    gateway.lookup_mcp_receipt.return_value = {"status": "FAILED", "receipt": None}
    gateway.lookup_mcp_approval.return_value = _validity()

    assert worker._execute_operation(context, plan, operation, {}) is None

    repository.halt_mcp_run.assert_not_called()
    repository.finish_mcp_attempt.assert_called_once_with(
        attempt.attempt_id, "failed", None, "EDIT_PERMISSION_DENIED"
    )


def test_mcp_receipt_rejects_version_or_identity_mismatch() -> None:
    worker, _, _, context, _, operation, command, attempt = _execution_fixture()
    identity = McpExecutionIdentity(
        command.approval_id, command.grant_id, command.client_id, command.policy_generation
    )
    for mutation in (
        {"before_version": 2}, {"operation_id": "other"}, {"after_version": 1}, {"changed": 1},
    ):
        with pytest.raises(ValueError):
            worker._validate_mcp_receipt(
                {**_receipt(command, attempt.attempt_id), **mutation},
                context,
                operation,
                operation.arguments,
                identity,
                attempt.attempt_id,
            )


def test_mcp_single_edit_budget_exhaustion_skips_without_llm_clarification() -> None:
    worker, repository, gateway, context, plan, _, _, _ = _execution_fixture()
    repository.load_context.return_value = context
    repository.load_current_plan.return_value = plan
    repository.load_operation_results.return_value = {}
    repository.remaining_tool_calls.return_value = 0

    result = worker._execute_step_node({
        "run_id": context.run.id,
        "plan_id": plan.id,
        "plan_version": plan.version,
        "operation_hash": plan.operation_hash,
        "steps": 0,
    })

    assert result["outcome"] == "verify"
    repository.mark_operation.assert_called_once_with(
        "op-1", ("pending",), "skipped", "react_tool_budget_insufficient"
    )
    repository.request_clarification.assert_not_called()
    gateway.execute.assert_not_called()


def test_mcp_twenty_operations_progress_with_existing_step_and_call_budget() -> None:
    run, _, operation, _ = _approval_fixture()
    run = replace(run, status="executing")
    operations = [
        replace(operation, id=f"op-{index}", sequence=index, target_id=f"doc_{index}",
                arguments={**operation.arguments, "document_id": f"doc_{index}"})
        for index in range(1, 21)
    ]
    plan = replace(build_agent_plan("plan-20", run.id, 1, "Twenty", tuple(operations)), status="approved")
    repository = MagicMock()
    context = AgentRunContext(run, None, ())
    repository.load_context.return_value = context
    repository.remaining_tool_calls.return_value = 40
    results: dict[str, dict[str, object]] = {}

    def current_plan(_run_id):
        return replace(plan, operations=tuple(operations))

    repository.load_current_plan.side_effect = current_plan
    repository.load_operation_results.side_effect = lambda *_: dict(results)
    worker = AgentWorker(repository, MagicMock(), MagicMock(), MagicMock())
    worker._active_execution_context = MagicMock(return_value=context)

    def execute(_context, _plan, selected, _results):
        operations[selected.sequence - 1] = replace(selected, status="succeeded")
        results[selected.id] = {"id": selected.target_id}
        return results[selected.id]

    worker._execute_operation = MagicMock(side_effect=execute)
    state = {
        "run_id": run.id, "plan_id": plan.id, "plan_version": plan.version,
        "operation_hash": plan.operation_hash, "steps": 0, "observations": [],
    }
    for _ in range(20):
        state.update(worker._execute_step_node(state))
        assert state["outcome"] == "continue"
    state.update(worker._execute_step_node(state))
    assert state["outcome"] == "verify"
    assert state["steps"] == 21
    assert worker._execute_operation.call_count == 20
    repository.request_clarification.assert_not_called()
