import unittest

from app.modules.agent_run.application.approve_agent_plan import ApproveAgentPlanUseCase
from app.modules.agent_run.domain.entities import AgentRun
from app.modules.agent_run.domain.plan import AgentPlanOperation, build_agent_plan


class InMemoryApprovalRepository:
    def __init__(self) -> None:
        operation = AgentPlanOperation(
            id="op-1",
            sequence=1,
            tool_name="move_document",
            target_type="document",
            target_id="document-1",
            base_version=1,
            source_parent_id="old",
            destination_parent_id="new",
            arguments={"destination_folder_id": "new"},
            reason="정리",
        )
        self.run = AgentRun(
            id="run-1",
            workspace_id="workspace-1",
            user_id="user-1",
            action="folder_organize",
            status="awaiting_approval",
            request_summary="정리",
            skill_version_id=None,
            current_plan_id="plan-1",
        )
        self.plan = build_agent_plan("plan-1", "run-1", 1, "정리", (operation,))
        self.approved = False

    def get_current_plan_for_user(self, workspace_id: str, user_id: str, run_id: str):
        if (workspace_id, user_id, run_id) != ("workspace-1", "user-1", "run-1"):
            return None
        return self.run, self.plan

    def approve_and_enqueue(self, run, plan, approval_id: str, job_id: str):
        self.approved = True
        return AgentRun(**{**run.__dict__, "status": "executing"})


class ApproveAgentPlanTest(unittest.TestCase):
    def test_approves_exact_plan_version_and_hash(self) -> None:
        repository = InMemoryApprovalRepository()
        use_case = ApproveAgentPlanUseCase(repository)  # type: ignore[arg-type]

        run = use_case.execute(
            workspace_id="workspace-1",
            user_id="user-1",
            run_id="run-1",
            plan_version=1,
            operation_hash=repository.plan.operation_hash,
        )

        self.assertTrue(repository.approved)
        self.assertEqual(run.status, "executing")

    def test_rejects_stale_version_or_hash(self) -> None:
        repository = InMemoryApprovalRepository()
        use_case = ApproveAgentPlanUseCase(repository)  # type: ignore[arg-type]

        for version, operation_hash in ((2, repository.plan.operation_hash), (1, "stale")):
            with self.subTest(version=version, operation_hash=operation_hash):
                with self.assertRaisesRegex(ValueError, "changed"):
                    use_case.execute(
                        workspace_id="workspace-1",
                        user_id="user-1",
                        run_id="run-1",
                        plan_version=version,
                        operation_hash=operation_hash,
                    )

        self.assertFalse(repository.approved)


if __name__ == "__main__":
    unittest.main()
