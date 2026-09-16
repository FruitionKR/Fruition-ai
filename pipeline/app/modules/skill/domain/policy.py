import re

from app.modules.skill.domain.entities import SkillCapability, SkillTool


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
