import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from app.modules.agent_run.domain.entities import McpProposalResult
from app.modules.agent_run.interfaces.http import routes


def test_oversized_proposal_stops_reading_before_remaining_body(monkeypatch):
    monkeypatch.setattr(routes, "_MCP_PROPOSAL_MAX_REQUEST_BYTES", 4)

    async def stream():
        yield b"123"
        yield b"45"
        raise AssertionError("oversized request must stop streaming")

    use_case = Mock()
    response = asyncio.run(routes.create_mcp_proposal(SimpleNamespace(stream=stream), use_case))
    assert response.status_code == 413
    use_case.execute.assert_not_called()


def test_sync_proposal_work_runs_outside_event_loop_thread(monkeypatch):
    command = object()
    monkeypatch.setattr(routes.McpAgentProposalRequest, "model_validate",
                        lambda _value: SimpleNamespace(to_application=lambda: command))
    event_loop_thread = threading.get_ident()

    async def stream():
        yield b"{}"

    def execute(value):
        assert value is command
        assert threading.get_ident() != event_loop_thread
        return McpProposalResult("run", "plan", 1, "a" * 64)

    response = asyncio.run(routes.create_mcp_proposal(
        SimpleNamespace(stream=stream), SimpleNamespace(execute=execute),
    ))
    assert response.run_id == "run"
