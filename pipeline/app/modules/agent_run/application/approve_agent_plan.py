from uuid import uuid4

from app.modules.agent_run.application.ports import AgentApprovalRepositoryPort
from app.modules.agent_run.domain.entities import AgentRun
from app.modules.agent_run.domain.plan import selected_operation_ids


class ApproveAgentPlanUseCase:
    def __init__(self, repository: AgentApprovalRepositoryPort) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        workspace_id: str,
        user_id: str,
        run_id: str,
        plan_version: int,
        operation_hash: str,
        operation_ids: list[str] | None = None,
    ) -> AgentRun:
        current = self._repository.get_current_plan_for_user(workspace_id, user_id, run_id)
        if current is None:
            raise ValueError("AgentRun or current plan not found.")
        run, plan = current
        if run.status != "awaiting_approval" or plan.status != "awaiting_approval":
            raise ValueError("Agent plan is not awaiting approval.")
        # 해시·버전은 부분 승인이어도 계획 전체 기준이다. 사용자가 본 계획 그대로인지 먼저 확인한다.
        if plan.version != plan_version or plan.operation_hash != operation_hash:
            raise ValueError("Agent plan changed and must be reviewed again.")
        selected = selected_operation_ids(plan, operation_ids)
        return self._repository.approve_and_enqueue(
            run,
            plan,
            approval_id=str(uuid4()),
            job_id=str(uuid4()),
            selected_operation_ids=selected,
        )
