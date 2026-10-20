from uuid import uuid4

from app.modules.skill.application.ports import ManageSkillRepositoryPort
from app.modules.skill.domain.entities import (
    Skill,
    SkillCapability,
    SkillResultMode,
    SkillScopeType,
    SkillTarget,
    SkillTool,
    SkillVersion,
    SkillVersionStatus,
)
from app.modules.skill.domain.policy import (
    CAPABILITY_TOOLS,
    canonical_allowed_tools,
    validate_allowed_tools,
    validate_skill_name,
)
from app.modules.skill.domain.safety import inspect_skill_instructions


class ManageSkillUseCase:
    def __init__(self, repository: ManageSkillRepositoryPort) -> None:
        self._repository = repository

    def require_manage_scope(self, workspace_id: str | None, user_id: str, scope_type: str) -> None:
        """팀 Skill은 작성(LLM 호출) 전에 관리 권한을 확인한다. 게시 때 거절하면 생성 비용만 쓰고 버려진다."""
        if scope_type != "personal" and not self._repository.can_manage_team(workspace_id, user_id):
            raise ValueError("Workspace Skill management is not allowed.")

    def preview(
        self,
        *,
        user_id: str,
        name: str,
        description: str,
        instructions_markdown: str,
        capabilities: tuple[SkillCapability, ...],
        allowed_tools: tuple[SkillTool, ...],
    ) -> SkillVersion:
        if not allowed_tools and capabilities and all(capability in CAPABILITY_TOOLS for capability in capabilities):
            # 게시와 같은 규칙: 비우면 capabilities로 계산한다.
            allowed_tools = canonical_allowed_tools(capabilities)
        _validate_definition(name, name, description, instructions_markdown, capabilities, allowed_tools)
        return _new_version(
            skill_id="preview",
            version=1,
            user_id=user_id,
            name=name,
            description=description,
            instructions_markdown=instructions_markdown,
            capabilities=capabilities,
            allowed_tools=allowed_tools,
        )

    def create_published(
        self,
        *,
        workspace_id: str,
        user_id: str,
        scope_type: SkillScopeType,
        slug: str,
        name: str,
        description: str,
        instructions_markdown: str,
        capabilities: tuple[SkillCapability, ...],
        allowed_tools: tuple[SkillTool, ...],
        targets: tuple[SkillTarget, ...] = (),
        result_mode: SkillResultMode | None = None,
        requires_source: bool = False,
    ) -> Skill:
        _validate_definition(slug, name, description, instructions_markdown, capabilities, allowed_tools)
        skill_id = str(uuid4())
        version = _new_version(
            skill_id=skill_id,
            version=1,
            user_id=user_id,
            name=name,
            description=description,
            instructions_markdown=instructions_markdown,
            capabilities=capabilities,
            allowed_tools=allowed_tools,
            targets=targets,
            result_mode=result_mode,
            requires_source=requires_source,
            status="published",
        )
        if _blocked_issues(version):
            raise ValueError("Skill version has blocked safety issues.")
        skill = Skill(
            id=skill_id,
            workspace_id=workspace_id if scope_type == "team" else None,
            scope_type=scope_type,
            owner_user_id=user_id if scope_type == "personal" else None,
            slug=slug,
            status="enabled",
            enabled_version=version,
            latest_version=version,
        )
        return self._repository.create_published(skill, version)

    def update_published(
        self,
        *,
        workspace_id: str,
        user_id: str,
        skill_id: str,
        name: str,
        description: str,
        instructions_markdown: str,
        capabilities: tuple[SkillCapability, ...],
        allowed_tools: tuple[SkillTool, ...],
        targets: tuple[SkillTarget, ...] = (),
        result_mode: SkillResultMode | None = None,
        requires_source: bool = False,
    ) -> Skill:
        skill = self.get_manageable(workspace_id, user_id, skill_id)
        _validate_definition(
            name,
            name,
            description,
            instructions_markdown,
            capabilities,
            allowed_tools,
        )
        latest_version = skill.latest_version or skill.enabled_version
        version = _new_version(
            skill_id=skill.id,
            version=(latest_version.version if latest_version else 0) + 1,
            user_id=user_id,
            name=name,
            description=description,
            instructions_markdown=instructions_markdown,
            capabilities=capabilities,
            allowed_tools=allowed_tools,
            targets=targets,
            result_mode=result_mode,
            requires_source=requires_source,
            status="published",
        )
        if _blocked_issues(version):
            raise ValueError("Skill version has blocked safety issues.")
        return self._repository.save_published_version(skill, version)

    def set_enabled(self, workspace_id: str, user_id: str, skill_id: str, enabled: bool) -> Skill:
        skill = self.get_manageable(workspace_id, user_id, skill_id)
        if enabled and skill.enabled_version is None:
            raise ValueError("Publish a Skill version before enabling it.")
        return self._repository.set_enabled(workspace_id, user_id, skill_id, enabled)

    def delete(self, workspace_id: str, user_id: str, skill_id: str) -> None:
        self._repository.delete(workspace_id, user_id, skill_id)

    def get_manageable(self, workspace_id: str, user_id: str, skill_id: str) -> Skill:
        skill = self._repository.get_manageable(workspace_id, user_id, skill_id)
        if skill is None:
            raise ValueError("Skill not found or not manageable.")
        return skill


def _validate_definition(
    slug: str,
    name: str,
    description: str,
    instructions_markdown: str,
    capabilities: tuple[SkillCapability, ...],
    allowed_tools: tuple[SkillTool, ...],
) -> None:
    validate_skill_name(slug)
    validate_skill_name(name)
    if slug != name:
        raise ValueError("Skill name and slug must match.")
    if not description.strip() or not instructions_markdown.strip():
        raise ValueError("name, description, and instructions_markdown are required.")
    if not capabilities or any(capability not in CAPABILITY_TOOLS for capability in capabilities):
        raise ValueError("capabilities must contain supported values.")
    validate_allowed_tools(capabilities, allowed_tools)


def _new_version(
    *,
    skill_id: str,
    version: int,
    user_id: str,
    name: str,
    description: str,
    instructions_markdown: str,
    capabilities: tuple[SkillCapability, ...],
    allowed_tools: tuple[SkillTool, ...],
    targets: tuple[SkillTarget, ...] = (),
    result_mode: SkillResultMode | None = None,
    requires_source: bool = False,
    status: SkillVersionStatus = "draft",
) -> SkillVersion:
    issues = inspect_skill_instructions(instructions_markdown)
    return SkillVersion(
        id=str(uuid4()),
        skill_id=skill_id,
        version=version,
        name=name.strip(),
        description=description.strip(),
        instructions_markdown=instructions_markdown.strip(),
        capabilities=capabilities,
        allowed_tools=allowed_tools,
        targets=targets,
        result_mode=result_mode,
        requires_source=requires_source,
        lint_result={"issues": [issue.__dict__ for issue in issues]},
        status=status,
        created_by=user_id,
    )


def _blocked_issues(version: SkillVersion) -> list[object]:
    lint_result = version.lint_result or {}
    issues = lint_result.get("issues", [])
    return [issue for issue in issues if isinstance(issue, dict) and issue.get("severity") == "blocked"]
