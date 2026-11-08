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


class McpSkillAcceptanceRouteTest(unittest.TestCase):
    def request(self, **definition_overrides: object):
        from app.modules.skill.interfaces.http.schemas import McpSkillAcceptanceRequest

        body = definition(**definition_overrides)
        return McpSkillAcceptanceRequest(
            run_id="run-1", operation_id="op-1", workspace_id=body.pop("workspace_id"), user_id=body.pop("user_id"),
            definition={**body, "capabilities": list(body["capabilities"]) if body["capabilities"] else None},
        )

    def test_replays_existing_evidence_without_saving(self) -> None:
        from app.modules.skill.infrastructure import postgres_skill_repository as store
        from app.modules.skill.interfaces.http import routes

        evidence = {"skill_id": "s", "version_id": "v", "version": 1, "created": True, "rolled_back": False}
        with patch.object(store, "find_mcp_skill_acceptance", return_value=evidence), \
                patch.object(store, "PostgresSkillRepository") as repository:
            result = routes.accept_mcp_skill_definition(self.request())
        self.assertEqual(result, {"status": "applied", "replayed": True, **evidence})
        repository.assert_not_called()

    def test_saves_with_mcp_parent_operation_and_returns_stored_evidence(self) -> None:
        from app.modules.skill.infrastructure import postgres_skill_repository as store
        from app.modules.skill.interfaces.http import routes

        memory = Repository()
        evidence = {"skill_id": "s", "version_id": "v", "version": 1, "created": True, "rolled_back": False}
        with patch.object(store, "find_mcp_skill_acceptance", side_effect=[None, evidence]), \
                patch.object(store, "PostgresSkillRepository", return_value=memory) as repository:
            result = routes.accept_mcp_skill_definition(self.request())
        repository.assert_called_once_with(parent_run_id="run-1", mcp_operation_id="op-1")
        self.assertEqual(result["replayed"], False)
        self.assertEqual(len(memory.skills), 1)

    def test_blocked_definition_is_rejected_without_saving(self) -> None:
        from fastapi import HTTPException

        from app.modules.skill.infrastructure import postgres_skill_repository as store
        from app.modules.skill.interfaces.http import routes

        memory = Repository()
        with patch.object(store, "find_mcp_skill_acceptance", return_value=None), \
                patch.object(store, "PostgresSkillRepository", return_value=memory):
            with self.assertRaises(HTTPException) as raised:
                routes.accept_mcp_skill_definition(self.request(instructions_markdown="AKIAABCDEFGHIJKLMNOP"))
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(memory.skills, {})

    def test_lock_replays_same_mcp_operation_and_rejects_other_operation(self) -> None:
        from app.modules.skill.domain.exceptions import SkillPublicationReplayedError
        from app.modules.skill.infrastructure.postgres_skill_repository import PostgresSkillRepository

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            "status": "executing", "result": {"skill_undo": {"operation_id": "op-1", "skill_id": "s"}}}
        with self.assertRaises(SkillPublicationReplayedError):
            PostgresSkillRepository("run-1", mcp_operation_id="op-1")._lock_parent(conn, "user-1")
        self.assertIn("origin = 'MCP_CLIENT'", conn.execute.call_args.args[0])
        with self.assertRaises(ValueError):
            PostgresSkillRepository("run-1", mcp_operation_id="op-2")._lock_parent(conn, "user-1")


if __name__ == "__main__":
    unittest.main()
