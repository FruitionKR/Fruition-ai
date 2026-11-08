from functools import lru_cache

from app.modules.agent_run.application.approve_agent_plan import ApproveAgentPlanUseCase
from app.modules.agent_run.application.mcp_approval import ApproveMcpAgentPlanUseCase
from app.modules.agent_run.application.mcp_proposal import CreateMcpProposalUseCase
from app.modules.agent_run.infrastructure.mcp_document_edit_context import (
    build_mcp_document_edit_context_reader,
)
from app.modules.agent_run.infrastructure.postgres_agent_run_repository import PostgresAgentRunRepository
from app.modules.agent_run.infrastructure.backend_tool_gateway import build_backend_tool_gateway


@lru_cache(maxsize=1)
def get_agent_run_repository() -> PostgresAgentRunRepository:
    return PostgresAgentRunRepository()


@lru_cache(maxsize=1)
def get_approve_agent_plan_use_case() -> ApproveAgentPlanUseCase:
    return ApproveAgentPlanUseCase(get_agent_run_repository())


@lru_cache(maxsize=1)
def get_approve_mcp_agent_plan_use_case() -> ApproveMcpAgentPlanUseCase:
    return ApproveMcpAgentPlanUseCase(get_agent_run_repository(), build_backend_tool_gateway())


@lru_cache(maxsize=1)
def get_create_mcp_proposal_use_case() -> CreateMcpProposalUseCase:
    from app.modules.skill.interfaces.http.dependencies import get_manage_skill_use_case

    return CreateMcpProposalUseCase(
        get_agent_run_repository(),
        build_mcp_document_edit_context_reader(),
        get_manage_skill_use_case(),
    )
