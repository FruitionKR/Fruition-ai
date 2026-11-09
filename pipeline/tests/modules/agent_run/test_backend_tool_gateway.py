import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.modules.agent_run.domain.entities import McpApprovalCommand, McpExecutionIdentity
from app.modules.agent_run.infrastructure.backend_tool_gateway import BackendToolGateway


def test_gateway_sends_agent_token_and_execution_contract() -> None:
    response = MagicMock()
    response.read.return_value = b'{"id":"folder-1"}'
    response.__enter__.return_value = response

    with patch(
        "app.modules.agent_run.infrastructure.backend_tool_gateway.urlopen",
        return_value=response,
    ) as urlopen:
        result = BackendToolGateway("http://document-svc:8080", "agent-token").execute(
            "create_folder",
            run_id="run-1",
            workspace_id="workspace-1",
            user_id="user-1",
            plan_id="plan-1",
            plan_version=1,
            operation_hash="a" * 64,
            operation_id="operation-1",
            idempotency_key="agent:run-1:plan-1:operation-1",
            arguments={"name": "새 폴더", "parent_folder_id": None},
        )

    request = urlopen.call_args.args[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert request.full_url.endswith("/internal/agent/tools/execute/create_folder")
    assert request.get_header("X-agent-service-token") == "agent-token"
    assert payload["operation_hash"] == "a" * 64
    assert payload["arguments"] == {"name": "새 폴더", "parent_folder_id": None}
    assert result == {"id": "folder-1"}


def test_gateway_sends_exact_mcp_execution_and_lookup_contracts() -> None:
    response = MagicMock()
    response.read.side_effect = [
        b'{"id":"doc-1"}',
        b'{"status":"VALID","reason":null,"lifecycle_status":"RESERVED",'
        b'"forward_status":"PENDING","settlement_status":"RESERVED"}',
        b'{"status":"UNKNOWN","receipt":null}',
    ]
    response.__enter__.return_value = response
    command = McpApprovalCommand(
        run_id="run-1", workspace_id="workspace-1", user_id="user-1", plan_version=1,
        operation_hash="a" * 64, operation_ids=("op-1",),
        approval_id="00000000-0000-4000-8000-000000000001", approval_mode="EXPLICIT_WEB",
        grant_id="00000000-0000-4000-8000-000000000002",
        client_id="https://client.example/metadata", policy_generation=3,
    )
    identity = McpExecutionIdentity(
        command.approval_id, command.grant_id, command.client_id, command.policy_generation
    )
    gateway = BackendToolGateway("http://document-svc:8080", "agent-token")

    with patch(
        "app.modules.agent_run.infrastructure.backend_tool_gateway.urlopen", return_value=response
    ) as urlopen:
        gateway.execute(
            "apply_document_edit", run_id=command.run_id, workspace_id=command.workspace_id,
            user_id=command.user_id, plan_id="plan-1", plan_version=command.plan_version,
            operation_hash=command.operation_hash, operation_id="op-1", idempotency_key="key",
            arguments={"document_id": "doc-1"}, approval_id=command.approval_id,
            attempt_id="00000000-0000-4000-8000-000000000003", grant_id=command.grant_id,
            client_id=command.client_id, policy_generation=command.policy_generation,
        )
        assert gateway.lookup_mcp_approval(command).status == "VALID"
        assert gateway.lookup_mcp_receipt(identity, "op-1", "workspace-1", "user-1") == {
            "status": "UNKNOWN", "receipt": None,
        }

    execute = json.loads(urlopen.call_args_list[0].args[0].data)
    assert execute["approval_id"] == command.approval_id
    assert execute["attempt_id"] == "00000000-0000-4000-8000-000000000003"
    approval_request = urlopen.call_args_list[1].args[0]
    assert approval_request.full_url.endswith("/internal/agent/mcp/approvals/lookup")
    assert set(json.loads(approval_request.data)) == {
        "approval_id", "run_id", "workspace_id", "user_id", "plan_version", "operation_hash",
        "operation_ids", "grant_id", "client_id", "policy_generation",
    }
    receipt_request = urlopen.call_args_list[2].args[0]
    assert receipt_request.full_url.endswith("/internal/agent/mcp/receipts/lookup")
    assert set(json.loads(receipt_request.data)) == {
        "approval_id", "operation_id", "workspace_id", "user_id", "grant_id", "client_id",
    }


def test_gateway_rejects_partial_mcp_execution_identity_before_transport() -> None:
    with pytest.raises(ValueError, match="complete"):
        BackendToolGateway("http://document-svc:8080", "agent-token").execute(
            "apply_document_edit", run_id="run-1", workspace_id="workspace-1", user_id="user-1",
            plan_id="plan-1", plan_version=1, operation_hash="a" * 64, operation_id="op-1",
            idempotency_key="key", arguments={}, approval_id=str(uuid4()),
        )
