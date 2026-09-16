from functools import lru_cache

from app.modules.agent_run.application.approve_agent_plan import ApproveAgentPlanUseCase
from app.modules.agent_run.infrastructure.postgres_agent_run_repository import PostgresAgentRunRepository


@lru_cache(maxsize=1)
def get_agent_run_repository() -> PostgresAgentRunRepository:
    return PostgresAgentRunRepository()


@lru_cache(maxsize=1)
def get_approve_agent_plan_use_case() -> ApproveAgentPlanUseCase:
    return ApproveAgentPlanUseCase(get_agent_run_repository())
