import unittest
from unittest.mock import MagicMock, patch

from app.modules.skill.application.author_skill import accept_external_skill_definition
from app.modules.skill.application.manage_skill import ManageSkillUseCase
from app.modules.skill.domain.entities import Skill, SkillVersion
from app.modules.skill.domain.exceptions import SkillVersionConflictError
from tests.modules.skill.test_manage_skill import InMemoryManageSkillRepository


class Repository(InMemoryManageSkillRepository):
    def __init__(self, team_manager: bool = False) -> None:
        super().__init__()
        self.team_manager = team_manager
        self.expected_versions: list[int | None] = []

    def can_manage_team(self, workspace_id: str | None, user_id: str) -> bool:
        return self.team_manager

    def save_published_version(self, skill: Skill, version: SkillVersion, expected_version: int | None = None) -> Skill:
        self.expected_versions.append(expected_version)
        return super().save_published_version(skill, version)


def definition(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = dict(
        workspace_id="ws-1",
        user_id="user-1",
        skill_id=None,
        expected_version=None,
        scope_type="personal",
        name="meeting-notes",
        description="회의록을 정리한다.",
        instructions_markdown="{문서}의 결정 사항을 표로 정리한다.",
        capabilities=("document-edit",),
    )
    values.update(overrides)
    return values


class ExternalSkillDefinitionTest(unittest.TestCase):
    def test_validates_without_storing_then_publishes_same_definition(self) -> None:
        repository = Repository()
        manager = ManageSkillUseCase(repository)

        checked = accept_external_skill_definition(manager, **definition())
        self.assertEqual(checked.status, "proposal_ready")
        self.assertEqual(repository.skills, {})

        published = accept_external_skill_definition(manager, **definition(), publish=True)
        self.assertEqual(published.status, "published")
        version = published.skill.enabled_version
        self.assertEqual(version.capabilities, ("document-edit",))
        self.assertIn("apply_document_edit", version.allowed_tools)
        self.assertEqual(version.targets, ("current_document",))
        self.assertEqual(checked.proposal, published.proposal)

    def test_team_scope_requires_skill_author(self) -> None:
        repository = Repository(team_manager=False)
        with self.assertRaises(ValueError):
            accept_external_skill_definition(ManageSkillUseCase(repository), **definition(scope_type="team"), publish=True)
        self.assertEqual(repository.skills, {})

    def test_safety_issues_block_without_storing(self) -> None:
        repository = Repository()
        result = accept_external_skill_definition(
            ManageSkillUseCase(repository),
            **definition(instructions_markdown="키 AKIAABCDEFGHIJKLMNOP 를 넣는다."),
            publish=True,
        )
        self.assertEqual(result.status, "blocked")
        self.assertTrue(result.issues)
        self.assertEqual(repository.skills, {})

    def test_update_requires_current_expected_version_and_keeps_capabilities(self) -> None:
        repository = Repository()
        manager = ManageSkillUseCase(repository)
        skill = accept_external_skill_definition(manager, **definition(), publish=True).skill

        with self.assertRaises(SkillVersionConflictError):
            accept_external_skill_definition(
                manager, **definition(skill_id=skill.id, expected_version=2, scope_type=None), publish=True
            )
        with self.assertRaises(ValueError):
            accept_external_skill_definition(manager, **definition(skill_id=skill.id, scope_type=None))

        updated = accept_external_skill_definition(
            manager,
            **definition(skill_id=skill.id, expected_version=1, scope_type=None, capabilities=None,
                         description="결정 사항만 정리한다."),
            publish=True,
        ).skill
        self.assertEqual(repository.expected_versions, [1])
        self.assertEqual(updated.enabled_version.capabilities, ("document-edit",))
        self.assertEqual(updated.enabled_version.description, "결정 사항만 정리한다.")

    def test_repository_rejects_stale_expected_version_inside_row_lock(self) -> None:
        from app.modules.skill.infrastructure import postgres_skill_repository as module

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {"version": 3}  # 다른 수정이 먼저 2를 저장했다.
        skill = Skill(id="skill-1", workspace_id=None, scope_type="personal", owner_user_id="user-1",
                      slug="meeting-notes", status="enabled")
        version = SkillVersion(id="v", skill_id="skill-1", version=2, name="meeting-notes", description="d",
                               instructions_markdown="i", capabilities=("document-edit",), created_by="user-1")
        with patch.object(module.database, "connect_ai") as connect, \
                patch.object(module, "_can_manage_team", return_value=False), \
                patch.object(module, "_lock_manageable", return_value=object()):
            connect.return_value.__enter__.return_value = conn
            with self.assertRaises(SkillVersionConflictError):
                module.PostgresSkillRepository().save_published_version(skill, version, expected_version=1)
        self.assertFalse(any("INSERT" in str(call) for call in conn.execute.call_args_list))


if __name__ == "__main__":
    unittest.main()
