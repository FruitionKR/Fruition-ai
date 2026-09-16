from dataclasses import dataclass, replace
import re

from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute, SkillCandidate
from app.modules.skill.application.ports import SkillRepositoryPort
from app.modules.skill.domain.entities import Skill
from app.modules.skill.domain.exceptions import SkillDisabledError, SkillNotFoundError


SLASH_COMMAND = re.compile(r"^/([a-z0-9][a-z0-9-]{0,62})(?:\s+(.+))?$", re.DOTALL)


@dataclass(frozen=True)
class ResolvedSkillRoute:
    route: AgentTurnRoute
    skill: Skill | None


@dataclass(frozen=True)
class PreparedSkillSelection:
    request: AgentTurnRequest
    skills: tuple[Skill, ...]
    explicit_skill_id: str | None = None

    def resolve_route(self, route: AgentTurnRoute) -> ResolvedSkillRoute:
        if route.action == "chat_answer":
            return ResolvedSkillRoute(route=replace(route, selected_skill_id=None), skill=None)

        selected_skill_id = self.explicit_skill_id or route.selected_skill_id
        selected = next((skill for skill in self.skills if skill.id == selected_skill_id), None)
        if selected is None or not _supports_route(selected, route):
            return ResolvedSkillRoute(route=replace(route, selected_skill_id=None), skill=None)
        return ResolvedSkillRoute(route=replace(route, selected_skill_id=selected.id), skill=selected)


class SelectSkillUseCase:
    def __init__(self, repository: SkillRepositoryPort, feature_enabled: bool = True) -> None:
        self._repository = repository
        self._feature_enabled = feature_enabled

    def prepare(self, request: AgentTurnRequest) -> PreparedSkillSelection:
        if not self._feature_enabled:
            if request.skill_mode == "explicit" or request.skill_id is not None or request.message.lstrip().startswith("/"):
                raise ValueError("Agent Skill 기능이 비활성화되어 있습니다.")
            return PreparedSkillSelection(request=replace(request, available_skills=()), skills=())
        if request.skill_mode == "off":
            return PreparedSkillSelection(request=replace(request, available_skills=()), skills=())
        if not request.workspace_id or not request.user_id:
            return PreparedSkillSelection(request=replace(request, available_skills=()), skills=())

        message, slash_slug = _parse_slash_command(request.message)
        explicit = request.skill_mode == "explicit" or request.skill_id is not None or slash_slug is not None
        if explicit:
            skill = self._explicit_skill(request, slash_slug)
            if skill.enabled_version is None:
                raise SkillDisabledError(skill.id)
            prepared = replace(
                request,
                message=message,
                skill_mode="explicit",
                skill_id=skill.id,
                available_skills=(_to_candidate(skill),),
            )
            return PreparedSkillSelection(prepared, (skill,), skill.id)

        skills = tuple(self._repository.list_accessible_enabled(request.workspace_id, request.user_id))
        return PreparedSkillSelection(
            request=replace(request, skill_mode="auto", available_skills=tuple(_to_candidate(skill) for skill in skills)),
            skills=skills,
        )

    def _explicit_skill(self, request: AgentTurnRequest, slash_slug: str | None) -> Skill:
        assert request.workspace_id is not None
        assert request.user_id is not None
        if request.skill_id:
            skill = self._repository.get_accessible(request.workspace_id, request.user_id, request.skill_id)
        elif slash_slug:
            skill = self._repository.get_accessible_by_slug(request.workspace_id, request.user_id, slash_slug)
        else:
            skill = None
        if skill is None:
            raise SkillNotFoundError(request.skill_id or slash_slug or "")
        return skill


def _parse_slash_command(message: str) -> tuple[str, str | None]:
    match = SLASH_COMMAND.fullmatch(message.strip())
    if match is None:
        return message, None
    return (match.group(2) or "").strip(), match.group(1)


def _to_candidate(skill: Skill) -> SkillCandidate:
    version = skill.enabled_version
    if version is None:
        raise ValueError("Enabled Skill must have an enabled version.")
    return SkillCandidate(
        id=skill.id,
        version_id=version.id,
        name=version.name,
        description=version.description,
        capabilities=version.capabilities,
    )


def _supports_route(skill: Skill, route: AgentTurnRoute) -> bool:
    version = skill.enabled_version
    if version is None:
        return False
    supported_capabilities = set(version.capabilities)
    if "template" in supported_capabilities:
        supported_capabilities.update({"document-create", "document-edit"})
    if not set(route.required_capabilities).issubset(supported_capabilities):
        return False
    action = route.action
    if action in {"markdown_create", "markdown_edit"} and "template" in version.capabilities:
        return True
    if action == "workspace_workflow":
        return bool(
            set(version.capabilities)
            & {"document-create", "document-edit", "folder-organize", "template"}
        )
    required_capability = {
        "markdown_create": "document-create",
        "markdown_edit": "document-edit",
        "folder_organize": "folder-organize",
    }.get(action)
    return required_capability is not None and required_capability in version.capabilities
