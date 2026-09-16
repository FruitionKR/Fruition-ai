from typing import cast

from app.modules.skill.application.ports import SkillDraftGeneratorPort
from app.modules.skill.domain.entities import (
    SkillCapability,
    SkillDraftProposal,
    SkillDraftSourceRun,
    SkillTool,
)
from app.modules.skill.domain.policy import (
    CAPABILITY_TOOLS,
    validate_allowed_tools,
    validate_skill_name,
    with_required_planning_reads,
)
from app.modules.skill.domain.safety import inspect_skill_instructions


class ProposeSkillDraftUseCase:
    def __init__(self, generator: SkillDraftGeneratorPort) -> None:
        self._generator = generator

    def execute(
        self,
        *,
        source_runs: tuple[SkillDraftSourceRun, ...],
        user_directives: tuple[str, ...],
        excluded_literals: tuple[str, ...],
    ) -> SkillDraftProposal:
        if not source_runs:
            raise ValueError("At least one completed AgentRun is required.")
        if any(source.status != "completed" for source in source_runs):
            raise ValueError("Skill draft sources must be completed AgentRuns.")
        if any(not source.successful_operations for source in source_runs):
            raise ValueError("Skill draft sources require successful operations.")

        candidate = self._generator.generate(source_runs, user_directives)
        name = _required_text(candidate, "name")
        name = validate_skill_name(name)
        description = _required_text(candidate, "description")
        instructions = _required_text(candidate, "instructions_markdown")
        capabilities = _capabilities(candidate.get("capabilities"))
        proposed_tools = _tools(candidate.get("allowed_tools"))
        successful_tools = {
            operation.tool_name
            for source in source_runs
            for operation in source.successful_operations
        }
        if not set(proposed_tools).issubset(successful_tools):
            raise ValueError("Skill proposal tools must come from successful operations.")
        allowed_tools = with_required_planning_reads(proposed_tools)
        validate_allowed_tools(capabilities, allowed_tools)
        if inspect_skill_instructions(instructions):
            raise ValueError("Skill proposal contains blocked safety instructions.")
        _reject_excluded_literals(
            (name, description, instructions),
            excluded_literals + tuple(source.run_id for source in source_runs),
        )
        return SkillDraftProposal(
            name=name,
            description=description,
            instructions_markdown=instructions,
            capabilities=capabilities,
            allowed_tools=allowed_tools,
            source_run_ids=tuple(source.run_id for source in source_runs),
        )


def _required_text(candidate: dict[str, object], key: str) -> str:
    value = candidate.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Skill proposal {key} is required.")
    return value.strip()


def _capabilities(value: object) -> tuple[SkillCapability, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError("Skill proposal capabilities are required.")
    if any(item not in CAPABILITY_TOOLS for item in value):
        raise ValueError("Skill proposal contains an unsupported capability.")
    return tuple(cast(SkillCapability, item) for item in value)


def _tools(value: object) -> tuple[SkillTool, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Skill proposal allowed_tools must be an array.")
    known_tools = {tool for tools in CAPABILITY_TOOLS.values() for tool in tools}
    if any(item not in known_tools for item in value):
        raise ValueError("Skill proposal contains an unsupported tool.")
    return tuple(cast(SkillTool, item) for item in value)


def _reject_excluded_literals(values: tuple[str, ...], excluded_literals: tuple[str, ...]) -> None:
    normalized_values = "\n".join(values).casefold()
    if any(
        literal.strip() and literal.strip().casefold() in normalized_values
        for literal in excluded_literals
    ):
        raise ValueError("Skill proposal contains a fixed resource value.")
