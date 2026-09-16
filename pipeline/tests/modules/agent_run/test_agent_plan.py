import unittest

from app.modules.agent_run.domain.plan import AgentPlanOperation, build_agent_plan


def operation(operation_id: str = "op-1", reason: str = "관련 문서를 이동") -> AgentPlanOperation:
    return AgentPlanOperation(
        id=operation_id,
        sequence=1,
        tool_name="move_document",
        target_type="document",
        target_id="document-1",
        base_version=3,
        source_parent_id="folder-old",
        destination_parent_id="folder-new",
        arguments={"destination_folder_id": "folder-new"},
        reason=reason,
    )


class AgentPlanTest(unittest.TestCase):
    def test_same_operations_have_same_canonical_hash(self) -> None:
        first = build_agent_plan("plan-1", "run-1", 1, "정리 계획", (operation(),))
        second = build_agent_plan("plan-2", "run-1", 1, "다른 요약", (operation(reason="다른 이유"),))

        self.assertEqual(first.operation_hash, second.operation_hash)

    def test_changed_operation_has_different_hash(self) -> None:
        first = build_agent_plan("plan-1", "run-1", 1, "정리 계획", (operation(),))
        changed = AgentPlanOperation(**{**operation().__dict__, "destination_parent_id": "folder-other"})
        second = build_agent_plan("plan-2", "run-1", 1, "정리 계획", (changed,))

        self.assertNotEqual(first.operation_hash, second.operation_hash)

    def test_rejects_more_than_twenty_operations(self) -> None:
        operations = tuple(
            AgentPlanOperation(**{**operation(f"op-{index}").__dict__, "sequence": index})
            for index in range(1, 22)
        )

        with self.assertRaisesRegex(ValueError, "20"):
            build_agent_plan("plan-1", "run-1", 1, "정리 계획", operations)

    def test_rejects_unknown_dependency(self) -> None:
        invalid = AgentPlanOperation(**{**operation().__dict__, "depends_on": ("missing",)})

        with self.assertRaisesRegex(ValueError, "dependency"):
            build_agent_plan("plan-1", "run-1", 1, "정리 계획", (invalid,))


if __name__ == "__main__":
    unittest.main()
