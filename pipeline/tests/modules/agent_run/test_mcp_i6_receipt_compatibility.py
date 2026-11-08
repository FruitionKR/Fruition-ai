from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from app.modules.agent_run.domain.plan import AgentPlanOperation, build_agent_plan
from app.modules.agent_run.infrastructure.agent_worker import AgentWorker, _resolve_operation_references
from tests.modules.agent_run.test_agent_rollback import Journal, Workspace
from tests.modules.agent_run.test_mcp_approval_execution import _execution_fixture


def test_committed_create_receipt_resolves_dependent_id_and_version():
    worker, repository, _, context, plan, operation, _, attempt = _execution_fixture()
    operation = replace(operation, tool_name="create_folder", target_type="folder")
    receipt = {"resource_id": "folder-created", "after_version": 1, "outcome": "COMMITTED"}

    result = worker._complete_mcp_receipt(context, plan, operation, attempt, "key", receipt)

    assert _resolve_operation_references(
        {"parent_folder_id": {"$operation_result": operation.id, "field": "id"},
         "base_version": {"$operation_result": operation.id, "field": "current_version"}},
        {operation.id: result},
    ) == {"parent_folder_id": "folder-created", "base_version": 1}
    assert repository.save_tool_execution.call_args.kwargs["response_metadata"] == result
    repository.finish_mcp_attempt.assert_called_once_with(attempt.attempt_id, "committed", receipt, None)
    assert "id" not in receipt


@pytest.mark.parametrize("tool,target,target_type,arguments", [
    ("create_folder", None, "folder", {"name": "new", "parent_folder_id": None}),
    ("rename_folder", "design", "folder", {"folder_id": "design", "name": "renamed", "base_version": 1}),
    ("move_folder", "design", "folder", {"folder_id": "design", "parent_folder_id": "inbox", "position": 0, "base_version": 1}),
    ("rename_document", "doc-0", "document", {"document_id": "doc-0", "display_name": "renamed", "base_version": 1}),
    ("move_document", "doc-0", "document", {"document_id": "doc-0", "folder_id": "inbox", "position": 0, "base_version": 1}),
])
def test_mcp_raw_receipt_supports_existing_reverse_compensation(tool, target, target_type, arguments):
    workspace = Workspace()
    before = workspace.state()
    operation = AgentPlanOperation("op", 1, tool, target_type, target, 1 if target else None,
                                   None, None, arguments, "change")
    plan = replace(build_agent_plan("plan", "run", 1, "change", (operation,)), status="approved")
    journal = Journal(plan)
    worker = AgentWorker(journal, MagicMock(), workspace, MagicMock())
    worker._execute_operation(journal.load_context("run"), plan, operation, {})
    forward = journal.records[operation.id]["forward_response"]
    journal.records[operation.id]["forward_response"] = {
        "resource_id": forward["id"], "after_version": forward["current_version"],
        "outcome": "COMMITTED", "changed": True,
    }
    journal.run = replace(journal.run, origin="MCP_CLIENT", status="cancel_requested")

    worker._rollback.execute("run")

    assert journal.run.status == "cancelled"
    assert workspace.state() == before
