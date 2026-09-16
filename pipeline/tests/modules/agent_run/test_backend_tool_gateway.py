import json
from unittest.mock import MagicMock, patch

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
