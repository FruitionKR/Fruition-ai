import unittest

from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute
from app.modules.skill.application.select_skill import SelectSkillUseCase
from app.modules.skill.domain.entities import Skill, SkillVersion
from app.modules.skill.domain.exceptions import SkillDisabledError, SkillNotFoundError


def enabled_skill(skill_id: str = "skill-1", slug: str = "organize") -> Skill:
    version = SkillVersion(
        id=f"{skill_id}-v1",
        skill_id=skill_id,
        version=1,
        name=slug,
        description="문서와 폴더를 목적에 맞게 정리합니다.",
        instructions_markdown="관련 문서를 한 폴더에 모은다.",
        capabilities=("folder-organize",),
        allowed_tools=("list_folder_children", "move_document"),
        status="published",
    )
    return Skill(
        id=skill_id,
        workspace_id="workspace-1",
        scope_type="team",
        owner_user_id=None,
        slug=slug,
        status="enabled",
        enabled_version=version,
    )


def personal_skill(skill_id: str = "personal-1", slug: str = "my-skill") -> Skill:
    version = SkillVersion(
        id=f"{skill_id}-v1",
        skill_id=skill_id,
        version=1,
        name=slug,
        description="개인 Skill",
        instructions_markdown="개인 규칙을 적용한다.",
        capabilities=(),
        status="published",
    )
    return Skill(
        id=skill_id,
        workspace_id=None,
        scope_type="personal",
        owner_user_id="user-1",
        slug=slug,
        status="enabled",
        enabled_version=version,
    )


class InMemorySkillRepository:
    def __init__(self, skills: list[Skill]) -> None:
        self.skills = skills

    @staticmethod
    def _is_accessible(skill: Skill, workspace_id: str, user_id: str) -> bool:
        return (skill.scope_type == "personal" and skill.owner_user_id == user_id) or (
            skill.scope_type == "team" and skill.workspace_id == workspace_id
        )

    def list_accessible_enabled(self, workspace_id: str, user_id: str) -> list[Skill]:
        return [
            skill
            for skill in self.skills
            if skill.status == "enabled"
            and self._is_accessible(skill, workspace_id, user_id)
        ]

    def get_accessible(self, workspace_id: str, user_id: str, skill_id: str) -> Skill | None:
        return next(
            (
                skill
                for skill in self.skills
                if skill.id == skill_id and self._is_accessible(skill, workspace_id, user_id)
            ),
            None,
        )

    def get_accessible_by_slug(self, workspace_id: str, user_id: str, slug: str) -> Skill | None:
        skills = [
            skill
            for skill in self.skills
            if skill.slug == slug and self._is_accessible(skill, workspace_id, user_id)
        ]
        return next((skill for skill in skills if skill.scope_type == "personal"), None) or (
            skills[0] if skills else None
        )


class SkillSelectionTest(unittest.TestCase):
    def test_personal_skill_is_available_in_another_workspace(self) -> None:
        use_case = SelectSkillUseCase(InMemorySkillRepository([personal_skill()]))  # type: ignore[arg-type]

        selection = use_case.prepare(
            AgentTurnRequest(
                message="개인 규칙을 적용해줘",
                workspace_id="workspace-2",
                user_id="user-1",
            )
        )

        self.assertIsNone(selection.skills[0].workspace_id)
        self.assertEqual(selection.request.available_skills[0].name, "my-skill")

    def test_disabled_feature_does_not_query_repository_in_auto_mode(self) -> None:
        class FailingRepository(InMemorySkillRepository):
            def list_accessible_enabled(self, workspace_id: str, user_id: str) -> list[Skill]:
                raise AssertionError("repository must not be queried")

        use_case = SelectSkillUseCase(FailingRepository([]), feature_enabled=False)

        selection = use_case.prepare(
            AgentTurnRequest(
                message="문서를 정리해줘",
                workspace_id="workspace-1",
                user_id="user-1",
            )
        )

        self.assertEqual(selection.request.available_skills, ())

    def test_disabled_feature_rejects_explicit_skill(self) -> None:
        use_case = SelectSkillUseCase(InMemorySkillRepository([]), feature_enabled=False)

        with self.assertRaisesRegex(ValueError, "비활성화"):
            use_case.prepare(
                AgentTurnRequest(
                    message="정리해줘",
                    workspace_id="workspace-1",
                    user_id="user-1",
                    skill_mode="explicit",
                    skill_id="skill-1",
                )
            )

    def test_template_skill_supports_markdown_create_and_edit(self) -> None:
        base = enabled_skill()
        assert base.enabled_version is not None
        template_version = SkillVersion(
            **{**base.enabled_version.__dict__, "capabilities": ("template",)}
        )
        template_skill = Skill(**{**base.__dict__, "enabled_version": template_version})
        use_case = SelectSkillUseCase(InMemorySkillRepository([template_skill]))  # type: ignore[arg-type]

        for action in ("markdown_create", "markdown_edit"):
            with self.subTest(action=action):
                selection = use_case.prepare(
                    AgentTurnRequest(
                        message="회사 템플릿을 적용해줘",
                        workspace_id="workspace-1",
                        user_id="user-1",
                        skill_mode="explicit",
                        skill_id="skill-1",
                    )
                )
                resolved = selection.resolve_route(
                    AgentTurnRoute(
                        action=action,  # type: ignore[arg-type]
                        confidence=0.9,
                        reason="template request",
                    )
                )

                self.assertEqual(resolved.skill, template_skill)

    def test_composite_route_requires_every_skill_capability(self) -> None:
        folder_skill = enabled_skill()
        assert folder_skill.enabled_version is not None
        composite_version = SkillVersion(
            **{
                **folder_skill.enabled_version.__dict__,
                "capabilities": ("document-edit", "folder-organize"),
            }
        )
        composite_skill = Skill(
            **{**folder_skill.__dict__, "enabled_version": composite_version}
        )
        route = AgentTurnRoute(
            action="workspace_workflow",
            confidence=0.9,
            reason="edit and move",
            document_operation="edit",
            persist=True,
            required_capabilities=("document-edit", "folder-organize"),
            selected_skill_id="skill-1",
        )

        partial = SelectSkillUseCase(InMemorySkillRepository([folder_skill])).prepare(
            AgentTurnRequest(
                message="문서를 요약한 뒤 옮겨줘",
                workspace_id="workspace-1",
                user_id="user-1",
            )
        )
        complete = SelectSkillUseCase(InMemorySkillRepository([composite_skill])).prepare(
            AgentTurnRequest(
                message="문서를 요약한 뒤 옮겨줘",
                workspace_id="workspace-1",
                user_id="user-1",
            )
        )

        self.assertIsNone(partial.resolve_route(route).skill)
        self.assertEqual(complete.resolve_route(route).skill, composite_skill)

    def test_off_mode_has_no_candidates(self) -> None:
        use_case = SelectSkillUseCase(InMemorySkillRepository([enabled_skill()]))  # type: ignore[arg-type]

        selection = use_case.prepare(
            AgentTurnRequest(
                message="폴더를 정리해줘",
                workspace_id="workspace-1",
                user_id="user-1",
                skill_mode="off",
            )
        )

        self.assertEqual(selection.request.available_skills, ())

    def test_explicit_id_takes_priority(self) -> None:
        use_case = SelectSkillUseCase(
            InMemorySkillRepository([enabled_skill("skill-1"), enabled_skill("skill-2", "other")])
        )  # type: ignore[arg-type]

        selection = use_case.prepare(
            AgentTurnRequest(
                message="정리해줘",
                workspace_id="workspace-1",
                user_id="user-1",
                skill_mode="explicit",
                skill_id="skill-2",
            )
        )

        self.assertEqual([candidate.id for candidate in selection.request.available_skills], ["skill-2"])
        self.assertEqual(selection.explicit_skill_id, "skill-2")

    def test_slash_command_resolves_slug_and_removes_command(self) -> None:
        use_case = SelectSkillUseCase(InMemorySkillRepository([enabled_skill()]))  # type: ignore[arg-type]

        selection = use_case.prepare(
            AgentTurnRequest(
                message="/organize 분기별 문서를 정리해줘",
                workspace_id="workspace-1",
                user_id="user-1",
            )
        )

        self.assertEqual(selection.request.message, "분기별 문서를 정리해줘")
        self.assertEqual(selection.explicit_skill_id, "skill-1")

    def test_missing_explicit_skill_is_an_error(self) -> None:
        use_case = SelectSkillUseCase(InMemorySkillRepository([]))  # type: ignore[arg-type]

        with self.assertRaises(SkillNotFoundError):
            use_case.prepare(
                AgentTurnRequest(
                    message="정리해줘",
                    workspace_id="workspace-1",
                    user_id="user-1",
                    skill_mode="explicit",
                    skill_id="missing",
                )
            )

    def test_auto_routing_disabled_skill_still_supports_explicit_use(self) -> None:
        skill = enabled_skill()
        disabled = Skill(**{**skill.__dict__, "status": "disabled"})
        use_case = SelectSkillUseCase(InMemorySkillRepository([disabled]))  # type: ignore[arg-type]

        selection = use_case.prepare(
            AgentTurnRequest(
                message="정리해줘",
                workspace_id="workspace-1",
                user_id="user-1",
                skill_mode="explicit",
                skill_id="skill-1",
            )
        )

        self.assertEqual(selection.explicit_skill_id, "skill-1")

    def test_unpublished_explicit_skill_is_an_error(self) -> None:
        skill = enabled_skill()
        unpublished = Skill(**{**skill.__dict__, "status": "disabled", "enabled_version": None})
        use_case = SelectSkillUseCase(InMemorySkillRepository([unpublished]))  # type: ignore[arg-type]

        with self.assertRaises(SkillDisabledError):
            use_case.prepare(
                AgentTurnRequest(
                    message="정리해줘",
                    workspace_id="workspace-1",
                    user_id="user-1",
                    skill_mode="explicit",
                    skill_id="skill-1",
                )
            )

    def test_query_route_never_selects_skill(self) -> None:
        use_case = SelectSkillUseCase(InMemorySkillRepository([enabled_skill()]))  # type: ignore[arg-type]
        selection = use_case.prepare(
            AgentTurnRequest(message="질문", workspace_id="workspace-1", user_id="user-1")
        )

        selected = selection.resolve_route(
            AgentTurnRoute(
                action="chat_answer",
                confidence=0.9,
                reason="question",
                selected_skill_id="skill-1",
            )
        )

        self.assertIsNone(selected.skill)
        self.assertIsNone(selected.route.selected_skill_id)


if __name__ == "__main__":
    unittest.main()
