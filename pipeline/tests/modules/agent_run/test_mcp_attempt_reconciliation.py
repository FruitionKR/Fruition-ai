from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from app.modules.agent_run.application.ports import ToolGatewayError
from app.modules.agent_run.infrastructure.mcp_attempt_reconciliation import reconcile_mcp_attempts
from tests.modules.agent_run.test_mcp_approval_execution import _execution_fixture, _receipt


def _pending():
    _, repository, gateway, context, plan, operation, command, attempt = _execution_fixture(False)
    repository.claim_mcp_reconciliation_batch.return_value = [{
        "attempt_id": attempt.attempt_id, "run_id": context.run.id,
        "plan_id": plan.id, "operation_id": operation.id,
    }]
    repository.execution_lock.return_value = nullcontext(True)
    repository.load_context.return_value = replace(context, run=replace(context.run, status="failed"))
    repository.load_current_plan.return_value = plan
    repository.get_undo_record.return_value = {"response_metadata": {"arguments": operation.arguments}}
    return repository, gateway, command, attempt


@pytest.mark.parametrize("outcome,status", [("COMMITTED", "committed"), ("NO_CHANGE", "no_change")])
def test_terminal_run_is_reconciled_only_from_matching_receipt(outcome, status):
    repository, gateway, command, attempt = _pending()
    receipt = _receipt(command, attempt.attempt_id, outcome)
    gateway.lookup_mcp_receipt.return_value = {"status": outcome, "receipt": receipt}

    assert reconcile_mcp_attempts(repository, gateway) == 1

    repository.record_mcp_reconciliation.assert_called_once_with(attempt.attempt_id, status, receipt)
    gateway.execute.assert_not_called()
    repository.mark_operation.assert_not_called()
    repository.mark_run_status.assert_not_called()
    repository.reserve_tool_call.assert_not_called()


@pytest.mark.parametrize("case", ["UNKNOWN", "NOT_FOUND", "transport", "wrong_attempt", "wrong_scope", "wrong_outcome"])
def test_missing_or_untrusted_receipt_remains_unresolved(case):
    repository, gateway, command, attempt = _pending()
    receipt = _receipt(command, attempt.attempt_id)
    lookup = {"status": "COMMITTED", "receipt": receipt}
    if case in {"UNKNOWN", "NOT_FOUND"}:
        lookup = {"status": case, "receipt": None}
    elif case == "transport":
        gateway.lookup_mcp_receipt.side_effect = ToolGatewayError(503, True)
    elif case == "wrong_attempt":
        receipt["attempt_id"] = "another-attempt"
    elif case == "wrong_scope":
        receipt["workspace_id"] = "another-workspace"
    else:
        lookup["status"] = "NO_CHANGE"
    gateway.lookup_mcp_receipt.return_value = lookup

    assert reconcile_mcp_attempts(repository, gateway) == 0
    repository.record_mcp_reconciliation.assert_not_called()
    gateway.execute.assert_not_called()


def test_authoritative_failed_lookup_is_definitive_without_execute():
    repository, gateway, _, attempt = _pending()
    gateway.lookup_mcp_receipt.return_value = {"status": "FAILED", "receipt": None}
    assert reconcile_mcp_attempts(repository, gateway) == 1
    repository.record_mcp_reconciliation.assert_called_once_with(attempt.attempt_id, "failed", None)
    gateway.execute.assert_not_called()


def test_active_execution_is_not_interrupted():
    repository, gateway, _, _ = _pending()
    repository.execution_lock.return_value = nullcontext(False)
    assert reconcile_mcp_attempts(repository, gateway) == 0
    gateway.lookup_mcp_receipt.assert_not_called()


def test_worker_maintenance_runs_without_new_jobs_and_obeys_interval(monkeypatch):
    import agent_worker

    reconcile = MagicMock()
    monkeypatch.setattr(agent_worker, "reconcile_mcp_attempts", reconcile)
    repository, gateway = MagicMock(), MagicMock()
    next_check = agent_worker._reconcile_mcp_attempts_if_due(repository, gateway, 0, 10)
    assert next_check == 70
    assert agent_worker._reconcile_mcp_attempts_if_due(repository, gateway, next_check, 20) == 70
    reconcile.assert_called_once_with(repository, gateway)
