import re

from app.modules.skill.domain.entities import SkillCapability, SkillResultMode, SkillTarget, SkillTool
from app.modules.skill.domain.exceptions import SkillRequestRejectedError


SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def validate_skill_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("Skill name is required.")
    if not SKILL_NAME_PATTERN.fullmatch(normalized):
        raise ValueError("Skill name must contain lowercase letters, numbers, or hyphens.")
    return normalized


READ_TOOLS: frozenset[SkillTool] = frozenset(
    {
        "list_root_items",
        "list_folder_children",
        "search_hierarchy",
        "get_breadcrumb",
        "get_document_metadata",
        "get_document_content",
    }
)
FOLDER_MUTATION_TOOLS: frozenset[SkillTool] = frozenset(
    {"create_folder", "rename_folder", "move_folder", "move_document", "rename_document"}
)
PLANNING_READ_TOOLS: tuple[SkillTool, ...] = (
    "list_root_items",
    "list_folder_children",
)
DOCUMENT_READ_TOOLS: frozenset[SkillTool] = frozenset(
    {"get_document_metadata", "get_document_content"}
)
DOCUMENT_MUTATION_TOOLS: frozenset[SkillTool] = frozenset(
    {"create_document", "apply_document_edit"}
)
CAPABILITY_TOOLS: dict[SkillCapability, frozenset[SkillTool]] = {
    "document-create": DOCUMENT_READ_TOOLS | frozenset(PLANNING_READ_TOOLS) | {"create_document"},
    "document-edit": DOCUMENT_READ_TOOLS | frozenset(PLANNING_READ_TOOLS) | {"apply_document_edit"},
    "folder-organize": READ_TOOLS | FOLDER_MUTATION_TOOLS,
    "template": DOCUMENT_READ_TOOLS | frozenset(PLANNING_READ_TOOLS) | DOCUMENT_MUTATION_TOOLS,
}


def canonical_allowed_tools(capabilities: tuple[SkillCapability, ...]) -> tuple[SkillTool, ...]:
    """capability별 canonical Tool의 합집합. 정렬해 같은 capability면 항상 같은 순서가 된다."""
    return with_required_planning_reads(
        tuple(sorted({tool for capability in capabilities for tool in CAPABILITY_TOOLS[capability]}))
    )


def validate_allowed_tools(
    capabilities: tuple[SkillCapability, ...],
    allowed_tools: tuple[SkillTool, ...],
) -> None:
    allowed_by_capability = frozenset(
        tool for capability in capabilities for tool in CAPABILITY_TOOLS[capability]
    )
    unsupported = set(allowed_tools) - allowed_by_capability
    if unsupported:
        raise ValueError(f"allowed_tools contains unsupported tools: {sorted(unsupported)}")
    required_reads = with_required_planning_reads(allowed_tools)
    missing_reads = set(required_reads) - set(allowed_tools)
    if missing_reads:
        raise ValueError(f"allowed_tools is missing planning read tools: {sorted(missing_reads)}")


def with_required_planning_reads(allowed_tools: tuple[SkillTool, ...]) -> tuple[SkillTool, ...]:
    mutation_tools = FOLDER_MUTATION_TOOLS | DOCUMENT_MUTATION_TOOLS
    if not mutation_tools.intersection(allowed_tools):
        return allowed_tools
    return tuple(
        dict.fromkeys((*PLANNING_READ_TOOLS, *allowed_tools))
    )


# targets·result_mode는 문서를 만들거나 고치는 Skill에서만 의미가 있다.
DOCUMENT_SKILL_CAPABILITIES: frozenset[SkillCapability] = frozenset({"document-create", "document-edit"})
SKILL_TARGET_ORDER: tuple[SkillTarget, ...] = ("current_document", "selection")
DEFAULT_SKILL_TARGETS: tuple[SkillTarget, ...] = ("current_document",)
RESULT_MODE_CAPABILITIES: dict[SkillResultMode, frozenset[SkillCapability]] = {
    "create": frozenset({"document-create"}),
    "edit": frozenset({"document-edit"}),
    "auto": frozenset({"document-create", "document-edit"}),
}


def is_document_skill(capabilities: tuple[SkillCapability, ...]) -> bool:
    return bool(DOCUMENT_SKILL_CAPABILITIES.intersection(capabilities))


def effective_targets(
    capabilities: tuple[SkillCapability, ...],
    targets: tuple[SkillTarget, ...],
) -> tuple[SkillTarget, ...]:
    """저장값이 비어 있는 문서 Skill(컬럼 추가 전 행 포함)은 기본 대상으로 읽는다."""
    if not is_document_skill(capabilities):
        return ()
    return targets or DEFAULT_SKILL_TARGETS


def result_mode_matches(capabilities: tuple[SkillCapability, ...], result_mode: SkillResultMode | None) -> bool:
    return result_mode is None or RESULT_MODE_CAPABILITIES[result_mode].issubset(capabilities)


def validate_execution_options(
    capabilities: tuple[SkillCapability, ...],
    targets: tuple[SkillTarget, ...] | None,
    result_mode: SkillResultMode | None,
) -> tuple[tuple[SkillTarget, ...], SkillResultMode | None]:
    """targets·result_mode를 capabilities와 맞춰 저장할 값으로 정규화한다.

    targets는 생략·빈 값이면 기본값이고, 순서는 SKILL_TARGET_ORDER로 고정한다.
    """
    if targets and len(set(targets)) != len(targets):
        raise SkillRequestRejectedError("invalid_targets", "targets must not contain duplicates.")
    if not is_document_skill(capabilities):
        if targets:
            raise SkillRequestRejectedError("invalid_targets", "targets require a document Skill capability.")
        if result_mode is not None:
            raise SkillRequestRejectedError(
                "invalid_result_mode",
                "result_mode requires a document Skill capability.",
            )
        return (), None
    if not result_mode_matches(capabilities, result_mode):
        raise SkillRequestRejectedError(
            "invalid_result_mode",
            f"result_mode {result_mode} does not match capabilities.",
        )
    ordered = tuple(target for target in SKILL_TARGET_ORDER if target in (targets or ()))
    return ordered or DEFAULT_SKILL_TARGETS, result_mode
