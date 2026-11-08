from unittest.mock import MagicMock

import pytest

from app.modules.agent_run.infrastructure.mcp_attempt_reconciliation import reconcile_mcp_attempts
from tests.modules.agent_run.test_agent_run_postgres import (
    _mcp_approval, _mcp_persist_values, _mcp_create_values, repositories, storage,  # noqa: F401
)
from tests.modules.agent_run.test_mcp_approval_execution import _receipt


def _unknown(repositories, key, *, create=False):
    runs, jobs, connect = repositories
    values = _mcp_create_values(key) if create else _mcp_persist_values(idempotency_key=key)
    runs.create_mcp_proposal(**values)
    command = _mcp_approval(values)
    runs.approve_mcp_and_enqueue(command, values["plan"], "job-" + key)
    operation = values["plan"].operations[0]
    jobs.mark_operation(operation.id, ("pending",), "running")
    jobs.prepare_undo(values["run"].id, values["plan"].id, operation.id,
                      operation.tool_name, operation.arguments, {})
    attempt = jobs.get_or_create_mcp_attempt(values["run"].id, values["plan"].id,
                                             operation.id, operation.tool_name, operation.arguments)
    jobs.save_tool_execution(
        run_id=values["run"].id, plan_id=values["plan"].id, operation_id=operation.id,
        tool_name=operation.tool_name,
        idempotency_key=f"agent:{values['run'].id}:{values['plan'].id}:{operation.id}",
        attempt=1, status="running", response_metadata={}, error_code=None,
    )
    jobs.finish_mcp_attempt(attempt.attempt_id, "unknown", None, "mcp_execution_unknown")
    with connect() as conn:
        conn.execute("UPDATE agent_runs SET status='failed', finished_at=now()-interval '100 days' WHERE id=%s",
                     (values["run"].id,))
        conn.execute("UPDATE agent_plan_operations SET status='failed', error_code='mcp_execution_unknown' WHERE id=%s",
                     (operation.id,))
        conn.execute("UPDATE agent_mcp_tool_attempts SET updated_at=now()-interval '2 minutes' WHERE attempt_id=%s",
                     (attempt.attempt_id,))
    receipt = _receipt(command, attempt.attempt_id)
    receipt["operation_id"] = operation.id
    if create:
        receipt.update(tool_name="create_document", before_version=None, after_version=1)
    return values, attempt, receipt


def test_late_receipt_updates_evidence_atomically_without_restarting_terminal_run(repositories, storage):
    values, attempt, receipt = _unknown(repositories, "late")
    runs, jobs, connect = repositories
    assert jobs.list_expired_run_ids() == ()
    assert jobs.delete_expired_runs((values["run"].id,)) == 0
    gateway = MagicMock()
    gateway.lookup_mcp_receipt.return_value = {"status": "COMMITTED", "receipt": receipt}

    assert reconcile_mcp_attempts(jobs, gateway) == 1

    with connect() as conn:
        assert conn.execute("SELECT status FROM agent_mcp_tool_attempts WHERE attempt_id=%s",
                            (attempt.attempt_id,)).fetchone()["status"] == "committed"
        forward = conn.execute("SELECT status,response_metadata FROM agent_tool_executions WHERE run_id=%s AND attempt=1",
                               (values["run"].id,)).fetchone()
        assert forward["status"] == "succeeded"
        assert forward["response_metadata"]["document_id"] == receipt["resource_id"]
        assert conn.execute("SELECT status FROM agent_plan_operations WHERE plan_id=%s",
                            (values["plan"].id,)).fetchone()["status"] == "failed"
    assert runs.get_for_user("workspace-1", "user-1", values["run"].id).status == "failed"
    assert jobs.list_expired_run_ids() == (values["run"].id,)
    gateway.execute.assert_not_called()


def test_unknown_batch_rotates_and_never_implies_failure(repositories, storage):
    _, first, _ = _unknown(repositories, "first")
    _, second, _ = _unknown(repositories, "second")
    _, jobs, connect = repositories
    batch1 = jobs.claim_mcp_reconciliation_batch(limit=1)
    batch2 = jobs.claim_mcp_reconciliation_batch(limit=1)
    assert {batch1[0]["attempt_id"], batch2[0]["attempt_id"]} == {first.attempt_id, second.attempt_id}
    assert jobs.claim_mcp_reconciliation_batch(limit=1) == []
    with connect() as conn:
        assert {row["status"] for row in conn.execute("SELECT status FROM agent_mcp_tool_attempts").fetchall()} == {"unknown"}


def test_forward_evidence_failure_rolls_back_receipt_resolution(repositories, storage):
    _, attempt, receipt = _unknown(repositories, "atomic")
    _, jobs, connect = repositories
    with connect() as conn:
        conn.execute("""CREATE FUNCTION reject_forward_update() RETURNS trigger LANGUAGE plpgsql AS $$
                        BEGIN RAISE EXCEPTION 'simulated write failure'; END $$""")
        conn.execute("CREATE TRIGGER reject_forward_update BEFORE UPDATE ON agent_tool_executions "
                     "FOR EACH ROW EXECUTE FUNCTION reject_forward_update()")
    with pytest.raises(Exception, match="simulated write failure"):
        jobs.record_mcp_reconciliation(attempt.attempt_id, "committed", receipt)
    with connect() as conn:
        assert conn.execute("SELECT status,receipt FROM agent_mcp_tool_attempts WHERE attempt_id=%s",
                            (attempt.attempt_id,)).fetchone() == {"status": "unknown", "receipt": None}


def test_late_create_receipt_is_projected_only_to_the_exact_held_operation(repositories, storage):
    _, original, receipt = _unknown(repositories, "request-1", create=True)
    runs, jobs, _ = repositories
    held = _mcp_create_values("request-2")
    runs.create_mcp_proposal(**held)
    runs.approve_mcp_and_enqueue(_mcp_approval(held), held["plan"], "held-job")
    operation = held["plan"].operations[0]
    jobs.mark_operation(operation.id, ("pending",), "running")
    jobs.prepare_undo(held["run"].id, held["plan"].id, operation.id, operation.tool_name, operation.arguments, {})
    blocked = jobs.get_or_create_mcp_attempt(held["run"].id, held["plan"].id, operation.id,
                                             operation.tool_name, operation.arguments)
    assert blocked.status == "held" and blocked.attempt_id == original.attempt_id
    jobs.mark_operation(operation.id, ("running",), "skipped", "mcp_create_pending_reconciliation")
    gateway = MagicMock()
    gateway.lookup_mcp_receipt.return_value = {"status": "COMMITTED", "receipt": receipt}

    assert reconcile_mcp_attempts(jobs, gateway) == 1

    projected = runs.get_current_plan_for_user("workspace-1", "user-1", held["run"].id)[1].operations[0]
    assert projected.status == "skipped"
    assert projected.existing_result == {"resource_type": "DOCUMENT", "resource_id": "doc_1", "current_version": 1}
    gateway.execute.assert_not_called()
