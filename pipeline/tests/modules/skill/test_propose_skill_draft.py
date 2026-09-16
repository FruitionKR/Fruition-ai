import unittest

from app.modules.skill.application.propose_skill_draft import ProposeSkillDraftUseCase
from app.modules.skill.domain.entities import (
    SkillDraftSourceOperation,
    SkillDraftSourceRun,
)


class FixedGenerator:
    def __init__(self, proposal: dict[str, object]) -> None:
        self.proposal = proposal
        self.source_runs: tuple[SkillDraftSourceRun, ...] = ()

    def generate(
        self,
        source_runs: tuple[SkillDraftSourceRun, ...],
        user_directives: tuple[str, ...],
    ) -> dict[str, object]:
        self.source_runs = source_runs
        return self.proposal


def completed_run() -> SkillDraftSourceRun:
    return SkillDraftSourceRun(
        run_id="run-1",
        status="completed",
        request_summary="Alpha 프로젝트 문서를 정리해줘",
        plan_summary="프로젝트별 폴더로 관련 문서를 이동합니다.",
        successful_operations=(
            SkillDraftSourceOperation(
                tool_name="create_folder",
                reason="프로젝트 폴더가 없어서 생성합니다.",
            ),
            SkillDraftSourceOperation(
                tool_name="move_document",
                reason="관련성이 명확한 문서만 이동합니다.",
            ),
        ),
    )


class ProposeSkillDraftUseCaseTest(unittest.TestCase):
    def test_returns_unsaved_proposal_limited_to_successful_tools(self) -> None:
        generator = FixedGenerator(
            {
                "name": "project-document-organizer",
                "description": "프로젝트별로 관련 문서를 정리합니다.",
                "instructions_markdown": "- 프로젝트 이름의 폴더가 없으면 생성한다.\n- 관련성이 명확한 문서만 이동한다.",
                "capabilities": ["folder-organize"],
                "allowed_tools": ["create_folder", "move_document"],
            }
        )

        proposal = ProposeSkillDraftUseCase(generator).execute(
            source_runs=(completed_run(),),
            user_directives=("관련성이 애매하면 이동하지 않는다.",),
            excluded_literals=("Alpha", "folder-id-1"),
        )

        self.assertEqual(proposal.source_run_ids, ("run-1",))
        self.assertEqual(
            proposal.allowed_tools,
            (
                "list_root_items",
                "list_folder_children",
                "create_folder",
                "move_document",
            ),
        )
        self.assertFalse(proposal.persisted)

    def test_rejects_non_completed_source(self) -> None:
        source = completed_run()
        invalid = SkillDraftSourceRun(
            run_id=source.run_id,
            status="partial_failed",
            request_summary=source.request_summary,
            plan_summary=source.plan_summary,
            successful_operations=source.successful_operations,
        )

        with self.assertRaisesRegex(ValueError, "completed"):
            ProposeSkillDraftUseCase(FixedGenerator({})).execute(
                source_runs=(invalid,),
                user_directives=(),
                excluded_literals=(),
            )

    def test_rejects_tools_not_observed_in_successful_operations(self) -> None:
        generator = FixedGenerator(
            {
                "name": "project-document-organizer",
                "description": "프로젝트별로 문서를 정리합니다.",
                "instructions_markdown": "관련 문서를 이동한다.",
                "capabilities": ["folder-organize"],
                "allowed_tools": ["rename_folder"],
            }
        )

        with self.assertRaisesRegex(ValueError, "successful"):
            ProposeSkillDraftUseCase(generator).execute(
                source_runs=(completed_run(),),
                user_directives=(),
                excluded_literals=(),
            )

    def test_rejects_excluded_resource_literals(self) -> None:
        generator = FixedGenerator(
            {
                "name": "alpha-document-organizer",
                "description": "프로젝트 문서를 정리합니다.",
                "instructions_markdown": "Alpha 폴더를 만든다.",
                "capabilities": ["folder-organize"],
                "allowed_tools": ["create_folder"],
            }
        )

        with self.assertRaisesRegex(ValueError, "fixed resource"):
            ProposeSkillDraftUseCase(generator).execute(
                source_runs=(completed_run(),),
                user_directives=(),
                excluded_literals=("Alpha",),
            )

    def test_rejects_approval_bypass_instruction(self) -> None:
        generator = FixedGenerator(
            {
                "name": "project-document-organizer",
                "description": "프로젝트 문서를 정리합니다.",
                "instructions_markdown": "승인 없이 관련 문서를 이동한다.",
                "capabilities": ["folder-organize"],
                "allowed_tools": ["move_document"],
            }
        )

        with self.assertRaisesRegex(ValueError, "safety"):
            ProposeSkillDraftUseCase(generator).execute(
                source_runs=(completed_run(),),
                user_directives=(),
                excluded_literals=(),
            )


if __name__ == "__main__":
    unittest.main()
