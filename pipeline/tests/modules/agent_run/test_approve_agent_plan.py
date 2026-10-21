import unittest

from app.modules.agent_run.application.approve_agent_plan import ApproveAgentPlanUseCase
from app.modules.agent_run.domain.entities import AgentRun
from app.modules.agent_run.domain.plan import (
    AgentPlanOperation,
    AgentPlanSelectionInvalid,
    build_agent_plan,
)


def _operation(operation_id: str, sequence: int, depends_on: tuple[str, ...] = ()) -> AgentPlanOperation:
    return AgentPlanOperation(
        id=operation_id,
        sequence=sequence,
        tool_name="move_document",
        target_type="document",
        target_id=f"document-{sequence}",
        base_version=1,
        source_parent_id="old",
        destination_parent_id="new",
        arguments={"destination_folder_id": "new"},
        reason="정리",
        depends_on=depends_on,
    )


class InMemoryApprovalRepository:
    def __init__(self, operations: tuple[AgentPlanOperation, ...] | None = None) -> None:
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
        self.plan = build_agent_plan("plan-1", "run-1", 1, "정리", operations or (_operation("op-1", 1),))
        self.approved = False
        self.selected: tuple[str, ...] | None = None

    def get_current_plan_for_user(self, workspace_id: str, user_id: str, run_id: str):
        if (workspace_id, user_id, run_id) != ("workspace-1", "user-1", "run-1"):
            return None
        return self.run, self.plan

    def approve_and_enqueue(self, run, plan, approval_id: str, job_id: str, selected_operation_ids=None):
        self.approved = True
        self.selected = selected_operation_ids
        return AgentRun(**{**run.__dict__, "status": "executing"})


def _three_operation_repository() -> InMemoryApprovalRepository:
    # op-2는 op-1에 의존한다. op-3은 독립이다.
    return InMemoryApprovalRepository(
        (_operation("op-1", 1), _operation("op-2", 2, ("op-1",)), _operation("op-3", 3))
    )


class ApproveAgentPlanTest(unittest.TestCase):
    def _approve(self, repository: InMemoryApprovalRepository, operation_ids: list[str] | None):
        return ApproveAgentPlanUseCase(repository).execute(  # type: ignore[arg-type]
            workspace_id="workspace-1",
            user_id="user-1",
            run_id="run-1",
            plan_version=1,
            operation_hash=repository.plan.operation_hash,
            operation_ids=operation_ids,
        )

    def test_approves_exact_plan_version_and_hash(self) -> None:
        repository = InMemoryApprovalRepository()

        run = self._approve(repository, None)

        self.assertTrue(repository.approved)
        self.assertIsNone(repository.selected)
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
                        operation_ids=["op-1"],
                    )

        self.assertFalse(repository.approved)

    def test_partial_approval_passes_selected_operations_in_plan_order(self) -> None:
        repository = _three_operation_repository()

        self._approve(repository, ["op-3", "op-1"])

        self.assertEqual(repository.selected, ("op-1", "op-3"))

    def test_selecting_every_operation_is_a_full_approval(self) -> None:
        repository = _three_operation_repository()

        self._approve(repository, ["op-2", "op-1", "op-3"])

        self.assertIsNone(repository.selected)

    def test_rejects_invalid_selection_without_approving(self) -> None:
        cases = {
            "empty": [],
            "duplicate": ["op-1", "op-1"],
            "unknown": ["op-1", "op-9"],
            "missing dependency": ["op-2", "op-3"],
        }
        for name, operation_ids in cases.items():
            with self.subTest(name):
                repository = _three_operation_repository()
                with self.assertRaises(AgentPlanSelectionInvalid):
                    self._approve(repository, operation_ids)
                self.assertFalse(repository.approved)


if __name__ == "__main__":
    unittest.main()
