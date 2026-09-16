import unittest
from unittest.mock import MagicMock

from app.modules.skill.application.manage_skill import ManageSkillUseCase
from app.modules.skill.domain.entities import Skill, SkillVersion
from app.modules.skill.infrastructure.postgres_skill_repository import (
    _lock_slug_scope,
    _with_next_version,
)


class InMemoryManageSkillRepository:
    def __init__(self) -> None:
        self.skills: dict[str, Skill] = {}

    def create_published(self, skill: Skill, version: SkillVersion) -> Skill:
        if any(
            existing.slug == skill.slug
            and (
                (skill.scope_type == "personal" and existing.scope_type == "personal" and existing.owner_user_id == skill.owner_user_id)
                or (skill.scope_type == "team" and existing.scope_type == "team" and existing.workspace_id == skill.workspace_id)
            )
            for existing in self.skills.values()
        ):
            raise ValueError("Skill command already exists in this scope.")
        saved = Skill(**{**skill.__dict__, "latest_version": version})
        self.skills[skill.id] = saved
        return saved

    def get_manageable(self, workspace_id: str, user_id: str, skill_id: str) -> Skill | None:
        return self.skills.get(skill_id)

    def save_published_version(self, skill: Skill, version: SkillVersion) -> Skill:
        saved = Skill(
            **{
                **skill.__dict__,
                "slug": version.name,
                "enabled_version": version,
                "latest_version": version,
            }
        )
        self.skills[skill.id] = saved
        return saved

    def set_enabled(self, workspace_id: str, user_id: str, skill_id: str, enabled: bool) -> Skill:
        skill = self.skills[skill_id]
        updated = Skill(**{**skill.__dict__, "status": "enabled" if enabled else "disabled"})
        self.skills[skill_id] = updated
        return updated


class ManageSkillTest(unittest.TestCase):
    def test_assigns_next_version_from_database_after_lock(self) -> None:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {"version": 4}
        version = SkillVersion(
            id="version-4",
            skill_id="skill-1",
            version=2,
            name="meeting-notes",
            description="회의록을 작성합니다.",
            instructions_markdown="회의 내용을 요약한다.",
            capabilities=(),
        )

        updated = _with_next_version(conn, version)

        self.assertEqual(updated.version, 4)
        conn.execute.assert_called_once_with(
            "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM skill_versions WHERE skill_id = %s",
            ("skill-1",),
        )

    def test_duplicate_lock_is_scoped_by_personal_owner_and_command(self) -> None:
        conn = MagicMock()
        skill = Skill(
            id="skill-1",
            workspace_id=None,
            scope_type="personal",
            owner_user_id="user-1",
            slug="meeting-notes",
            status="enabled",
        )

        _lock_slug_scope(conn, skill, "meeting-notes")

        conn.execute.assert_called_once_with(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("skill-command:personal:user-1:meeting-notes",),
        )

    def test_creates_personal_skill_published_and_auto_routing_enabled(self) -> None:
        use_case = ManageSkillUseCase(InMemoryManageSkillRepository())  # type: ignore[arg-type]

        skill = use_case.create_published(
            workspace_id="workspace-1",
            user_id="user-1",
            scope_type="personal",
            slug="concise-document",
            name="concise-document",
            description="문서를 간결하게 작성합니다.",
            instructions_markdown="핵심만 작성한다.",
            capabilities=("document-create",),
            allowed_tools=("list_root_items", "list_folder_children", "create_document"),
        )

        self.assertIsNone(skill.workspace_id)
        self.assertEqual(skill.owner_user_id, "user-1")
        self.assertEqual(skill.status, "enabled")
        self.assertEqual(skill.enabled_version.status, "published")  # type: ignore[union-attr]

    def test_update_publishes_new_version_without_reenabling_auto_routing(self) -> None:
        repository = InMemoryManageSkillRepository()
        use_case = ManageSkillUseCase(repository)  # type: ignore[arg-type]
        skill = use_case.create_published(
            workspace_id="workspace-1",
            user_id="user-1",
            scope_type="personal",
            slug="concise-document",
            name="concise-document",
            description="문서를 간결하게 작성합니다.",
            instructions_markdown="핵심만 작성한다.",
            capabilities=("document-create",),
            allowed_tools=("list_root_items", "list_folder_children", "create_document"),
        )
        skill = use_case.set_enabled("workspace-1", "user-1", skill.id, False)

        updated = use_case.update_published(
            workspace_id="workspace-1",
            user_id="user-1",
            skill_id=skill.id,
            name="concise-document",
            description="문서를 더 간결하게 작성합니다.",
            instructions_markdown="핵심만 두 문단으로 작성한다.",
            capabilities=("document-create",),
            allowed_tools=("list_root_items", "list_folder_children", "create_document"),
        )

        self.assertEqual(updated.status, "disabled")
        self.assertEqual(updated.enabled_version.status, "published")  # type: ignore[union-attr]
        self.assertEqual(updated.enabled_version.version, 2)  # type: ignore[union-attr]

    def test_rejects_published_creation_when_safety_issue_is_blocked(self) -> None:
        use_case = ManageSkillUseCase(InMemoryManageSkillRepository())  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "blocked"):
            use_case.create_published(
                workspace_id="workspace-1",
                user_id="user-1",
                scope_type="personal",
                slug="unsafe-skill",
                name="unsafe-skill",
                description="승인 우회를 시도합니다.",
                instructions_markdown="사용자 승인 없이 바로 실행한다.",
                capabilities=("folder-organize",),
                allowed_tools=("list_root_items", "list_folder_children", "move_document"),
            )

    def test_rejects_unknown_tool_before_storage(self) -> None:
        use_case = ManageSkillUseCase(InMemoryManageSkillRepository())  # type: ignore[arg-type]

        with self.assertRaisesRegex(ValueError, "allowed_tools"):
            use_case.create_published(
                workspace_id="workspace-1",
                user_id="user-1",
                scope_type="team",
                slug="unsafe-tool",
                name="unsafe-tool",
                description="허용되지 않은 tool",
                instructions_markdown="정리한다.",
                capabilities=("folder-organize",),
                allowed_tools=("shell",),  # type: ignore[arg-type]
            )

    def test_rejects_skill_without_routable_capability(self) -> None:
        use_case = ManageSkillUseCase(InMemoryManageSkillRepository())  # type: ignore[arg-type]

        with self.assertRaisesRegex(ValueError, "supported values"):
            use_case.create_published(
                workspace_id="workspace-1",
                user_id="user-1",
                scope_type="personal",
                slug="unroutable-skill",
                name="unroutable-skill",
                description="어떤 Agent 작업에도 연결되지 않습니다.",
                instructions_markdown="지침만 저장한다.",
                capabilities=(),
                allowed_tools=(),
            )

    def test_rejects_tool_outside_capability_when_updating(self) -> None:
        repository = InMemoryManageSkillRepository()
        use_case = ManageSkillUseCase(repository)  # type: ignore[arg-type]
        skill = use_case.create_published(
            workspace_id="workspace-1",
            user_id="user-1",
            scope_type="personal",
            slug="concise-document",
            name="concise-document",
            description="문서를 간결하게 작성합니다.",
            instructions_markdown="핵심만 작성한다.",
            capabilities=("document-create",),
            allowed_tools=(),
        )

        with self.assertRaisesRegex(ValueError, "allowed_tools"):
            use_case.update_published(
                workspace_id="workspace-1",
                user_id="user-1",
                skill_id=skill.id,
                name="concise-document",
                description="문서를 정리합니다.",
                instructions_markdown="핵심만 작성한다.",
                capabilities=("document-create",),
                allowed_tools=("move_document",),
            )

    def test_rejects_non_english_name(self) -> None:
        use_case = ManageSkillUseCase(InMemoryManageSkillRepository())  # type: ignore[arg-type]

        with self.assertRaisesRegex(ValueError, "lowercase letters"):
            use_case.create_published(
                workspace_id="workspace-1",
                user_id="user-1",
                scope_type="personal",
                slug="concise-document",
                name="간결한 문서",
                description="문서를 간결하게 작성합니다.",
                instructions_markdown="핵심만 작성한다.",
                capabilities=("document-create",),
                allowed_tools=(),
            )

    def test_rejects_different_name_and_slug(self) -> None:
        use_case = ManageSkillUseCase(InMemoryManageSkillRepository())  # type: ignore[arg-type]

        with self.assertRaisesRegex(ValueError, "must match"):
            use_case.create_published(
                workspace_id="workspace-1",
                user_id="user-1",
                scope_type="personal",
                slug="meeting-notes",
                name="meeting-summary",
                description="회의 내용을 정리합니다.",
                instructions_markdown="핵심만 작성한다.",
                capabilities=(),
                allowed_tools=(),
            )

    def test_prevents_duplicate_commands_only_in_the_same_scope(self) -> None:
        repository = InMemoryManageSkillRepository()
        use_case = ManageSkillUseCase(repository)  # type: ignore[arg-type]
        create = dict(
            description="문서를 정리합니다.",
            instructions_markdown="핵심만 작성한다.",
            capabilities=("document-create",),
            allowed_tools=("list_root_items", "list_folder_children", "create_document"),
            slug="meeting-notes",
            name="meeting-notes",
        )

        use_case.create_published(
            workspace_id="workspace-1", user_id="user-1", scope_type="personal", **create
        )
        with self.assertRaisesRegex(ValueError, "already exists"):
            use_case.create_published(
                workspace_id="workspace-2", user_id="user-1", scope_type="personal", **create
            )

        use_case.create_published(
            workspace_id="workspace-2", user_id="user-2", scope_type="personal", **create
        )
        use_case.create_published(
            workspace_id="workspace-1", user_id="user-1", scope_type="team", **create
        )
        with self.assertRaisesRegex(ValueError, "already exists"):
            use_case.create_published(
                workspace_id="workspace-1", user_id="user-2", scope_type="team", **create
            )
        use_case.create_published(
            workspace_id="workspace-2", user_id="user-2", scope_type="team", **create
        )


if __name__ == "__main__":
    unittest.main()
