import copy

import pytest

from app.modules.agent_run.application.mcp_proposal import (
    CreateMcpProposalUseCase,
    McpProposalError,
    mcp_proposal_request_hash,
)
from app.modules.agent_run.domain.entities import McpProposalResult
from app.modules.agent_run.interfaces.http.schemas import McpAgentProposalRequest
from app.modules.skill.application.manage_skill import ManageSkillUseCase
from tests.modules.skill.test_external_skill_definition import Repository as SkillRepository


class MemoryRepository:
    def __init__(self) -> None:
        self.created: dict[str, object] | None = None

    def find_mcp_proposal(self, actor, idempotency_key, request_hash):
        return None

    def create_mcp_proposal(self, **values):
        self.created = values
        plan = values["plan"]
        return McpProposalResult(values["run"].id, plan.id, plan.version, plan.operation_hash)


def body(**definition: object) -> dict[str, object]:
    return {
        "schema_version": "mcp.agent-proposal.v1",
        "actor": {
            "invocation_id": "00000000-0000-4000-8000-000000000002",
            "grant_id": "00000000-0000-4000-8000-000000000001",
            "user_id": "user-1",
            "workspace_id": "workspace-1",
            "client_id": "https://client.example/metadata",
            "policy_generation": 1,
            "origin": "MCP_CLIENT",
            "spec_version": "2025-11-25",
            "tool_name": "propose_skill_definition",
        },
        "idempotency_key": "request-1",
        "request_hash": "sha256:" + "0" * 64,
        "proposal": {
            "request_summary": "회의록 Skill 만들기",
            "definition": {
                "skill_id": None, "expected_version": None, "scope_type": "personal",
                "name": "meeting-notes", "description": "회의록을 정리한다.",
                "instructions_markdown": "{문서}의 결정 사항을 표로 정리한다.",
                "capabilities": ["document-edit"], "targets": None, "result_mode": None,
                "requires_source": None, **definition,
            },
        },
    }


def command(raw: dict[str, object]):
    parsed = McpAgentProposalRequest.model_validate(raw).to_application()
    raw = copy.deepcopy(raw)
    raw["request_hash"] = mcp_proposal_request_hash(parsed)
    return McpAgentProposalRequest.model_validate(raw).to_application()


def use_case(repository: MemoryRepository, skills: SkillRepository | None = None) -> CreateMcpProposalUseCase:
    return CreateMcpProposalUseCase(repository, None, ManageSkillUseCase(skills or SkillRepository()))  # type: ignore[arg-type]


def test_creates_single_skill_operation_bound_to_canonical_definition_without_saving() -> None:
    repository, skills = MemoryRepository(), SkillRepository()

    result = use_case(repository, skills).execute(command(body()))

    plan = repository.created["plan"]  # type: ignore[index]
    assert result.status == "awaiting_approval"
    assert repository.created["artifacts"] == ()  # type: ignore[index]
    (operation,) = plan.operations
    assert (operation.tool_name, operation.target_type, operation.target_id, operation.base_version) == (
        "publish_skill_definition", "skill", None, None)
    assert operation.arguments["targets"] == ["current_document"]
    assert "apply_document_edit" in operation.arguments["allowed_tools"]
    assert skills.skills == {}


def test_rejects_mismatched_request_hash_and_unsafe_definition() -> None:
    with pytest.raises(McpProposalError):
        use_case(MemoryRepository()).execute(McpAgentProposalRequest.model_validate(body()).to_application())
    with pytest.raises(McpProposalError) as raised:
        use_case(MemoryRepository()).execute(command(body(instructions_markdown="AKIAABCDEFGHIJKLMNOP")))
    assert raised.value.code == "INVALID_INPUT"


def test_team_skill_without_author_permission_is_rejected() -> None:
    with pytest.raises(McpProposalError):
        use_case(MemoryRepository(), SkillRepository(team_manager=False)).execute(command(body(scope_type="team")))


def test_actor_tool_must_match_skill_proposal_shape() -> None:
    raw = body()
    raw["actor"]["tool_name"] = "propose_plan"  # type: ignore[index]
    with pytest.raises(Exception):
        McpAgentProposalRequest.model_validate(raw)


def test_rollback_of_skill_operation_uses_stored_skill_undo_evidence() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.modules.agent_run.application.rollback_agent_run import RollbackAgentRunUseCase

    repository = MagicMock()
    repository.mark_run_status.return_value = True
    repository.load_context.return_value = SimpleNamespace(
        run=SimpleNamespace(action="workspace_workflow", origin="MCP_CLIENT"))
    repository.load_undo_records.return_value = [{
        "id": "undo-1", "operation_id": "op-1", "tool_name": "publish_skill_definition",
        "forward_status": "unknown", "status": "pending", "response_metadata": {},
    }]
    repository.parent_rollback_error.return_value = None
    RollbackAgentRunUseCase(repository, MagicMock(), MagicMock()).execute("run-1")
    repository.parent_rollback_error.assert_called_once_with("run-1")
    repository.complete_undo.assert_called_once_with("undo-1", {})
    repository.finish_rollback.assert_called_once_with("run-1", None)

    repository.reset_mock()
    repository.parent_rollback_error.return_value = "rollback_skill_conflict"
    RollbackAgentRunUseCase(repository, MagicMock(), MagicMock()).execute("run-1")
    repository.finish_rollback.assert_called_once_with("run-1", "rollback_skill_conflict:op-1")
    repository.complete_undo.assert_not_called()
