from dataclasses import replace
from typing import cast

from app.modules.skill.application.manage_skill import ManageSkillUseCase
from app.modules.skill.application.ports import (
    SkillAuthoringGeneratorPort,
    SkillReferenceReaderPort,
)
from app.modules.skill.domain.entities import (
    SkillAuthoringMode,
    SkillAuthoringProposal,
    SkillAuthoringReference,
    SkillAuthoringResult,
    SkillCapability,
    SkillDraftProposal,
    SkillScopeType,
    SkillTool,
)
from app.modules.skill.domain.policy import (
    CAPABILITY_TOOLS,
    validate_allowed_tools,
    validate_skill_name,
)
from app.modules.skill.domain.reference_template import (
    build_reference_template_instructions,
    extract_fixed_reference_template,
    extract_markdown_structure,
)
from app.modules.skill.domain.safety import SkillSafetyIssue, inspect_skill_instructions


MAX_INSTRUCTION_CHARS = 4_000
MAX_REFERENCE_COUNT = 3
MAX_REFERENCE_CHARS = 40_000
MAX_TOTAL_REFERENCE_CHARS = 80_000
MAX_DESCRIPTION_CHARS = 500
MAX_INSTRUCTIONS_CHARS = 30_000
MAX_INSTRUCTIONS_LINES = 500
MAX_QUESTION_CHARS = 500
MAX_LLM_ISSUES = 10


class AuthorSkillUseCase:
    def __init__(
        self,
        generator: SkillAuthoringGeneratorPort,
        reference_reader: SkillReferenceReaderPort,
        skill_manager: ManageSkillUseCase,
    ) -> None:
        self._generator = generator
        self._reference_reader = reference_reader
        self._skill_manager = skill_manager

    def execute(
        self,
        *,
        workspace_id: str,
        user_id: str,
        scope_type: SkillScopeType,
        instruction: str,
        reference_document_ids: tuple[str, ...],
        allow_clarification: bool = True,
        name: str | None = None,
        description: str | None = None,
        authoring_mode: SkillAuthoringMode = "enhance",
        preserved_capabilities: tuple[SkillCapability, ...] | None = None,
        preserved_allowed_tools: tuple[SkillTool, ...] | None = None,
    ) -> SkillAuthoringResult:
        instruction = instruction.strip()
        max_instruction_chars = (
            MAX_INSTRUCTIONS_CHARS if authoring_mode == "preserve" else MAX_INSTRUCTION_CHARS
        )
        if not instruction or len(instruction) > max_instruction_chars:
            raise ValueError(f"instruction must contain 1-{max_instruction_chars} characters.")
        if len(reference_document_ids) > MAX_REFERENCE_COUNT:
            raise ValueError(f"reference_document_ids supports at most {MAX_REFERENCE_COUNT} documents.")
        if len(set(reference_document_ids)) != len(reference_document_ids):
            raise ValueError("reference_document_ids must not contain duplicates.")
        if any(not document_id.strip() for document_id in reference_document_ids):
            raise ValueError("reference_document_ids must contain non-empty ids.")
        input_issues = _tag_issues(inspect_skill_instructions(instruction), "instruction")
        if input_issues and authoring_mode != "regenerate":
            return SkillAuthoringResult(status="blocked", issues=input_issues)
        if input_issues:
            instruction = _redact_issues(instruction, input_issues)
        if name is not None:
            name = validate_skill_name(name)
        if description is not None:
            description = description.strip()
            if not description or len(description) > MAX_DESCRIPTION_CHARS:
                raise ValueError(f"description must contain 1-{MAX_DESCRIPTION_CHARS} characters.")
            description_issues = _tag_issues(
                inspect_skill_instructions(description),
                "description",
            )
            if description_issues:
                if authoring_mode != "regenerate":
                    return SkillAuthoringResult(status="blocked", issues=description_issues)
                description = None

        references = tuple(
            self._reference_reader.read(
                workspace_id=workspace_id,
                user_id=user_id,
                document_id=document_id,
            )
            for document_id in reference_document_ids
        )
        reference_issues = _validate_references(references)
        if reference_issues:
            if authoring_mode != "regenerate":
                return SkillAuthoringResult(status="blocked", issues=reference_issues)
            instruction, description, references = _redact_sources(
                instruction,
                description,
                references,
                reference_issues,
            )

        if preserved_capabilities is None:
            intent = _classify_intent(
                self._generator,
                instruction,
                references,
                description,
            )
            if intent is None:
                if allow_clarification:
                    return SkillAuthoringResult(
                        status="clarification_required",
                        question="이 Skill이 수행할 작업이 문서 작성, 문서 수정, 폴더 정리, 템플릿 중 무엇인지 알려 주세요.",
                    )
                raise ValueError("Skill request could not be classified.")
            capabilities, reference_mode, allowed_tools = intent
        else:
            reference_mode = "none"
            allowed_tools = preserved_allowed_tools or ()
            capabilities = preserved_capabilities
            if not capabilities or any(capability not in CAPABILITY_TOOLS for capability in capabilities):
                raise ValueError("Skill draft contains an unsupported capability.")
        candidate = self._generator.generate(
            instruction,
            references,
            allow_clarification=allow_clarification,
            authoring_mode=authoring_mode,
            requested_name=name,
            requested_description=description,
            reference_mode=reference_mode,
        )
        status = candidate.get("status")
        if status == "blocked":
            issues = _llm_safety_issues(
                candidate.get("issues"),
                instruction=instruction,
                description=description,
                references=references,
            )
            if authoring_mode != "regenerate":
                return SkillAuthoringResult(status="blocked", issues=issues)
            instruction, description, references = _redact_sources(
                instruction,
                description,
                references,
                issues,
            )
            intent = _classify_intent(
                self._generator,
                instruction,
                references,
                description,
            )
            if intent is None:
                if allow_clarification:
                    return SkillAuthoringResult(
                        status="clarification_required",
                        question="이 Skill이 수행할 작업이 문서 작성, 문서 수정, 폴더 정리, 템플릿 중 무엇인지 알려 주세요.",
                    )
                raise ValueError("Skill request could not be classified.")
            capabilities, reference_mode, allowed_tools = intent
            candidate = self._generator.generate(
                instruction,
                references,
                allow_clarification=allow_clarification,
                authoring_mode=authoring_mode,
                requested_name=name,
                requested_description=description,
                reference_mode=reference_mode,
            )
            status = candidate.get("status")
            if status == "blocked":
                return SkillAuthoringResult(
                    status="blocked",
                    issues=_llm_safety_issues(
                        candidate.get("issues"),
                        instruction=instruction,
                        description=description,
                        references=references,
                    ),
                )
        if status == "clarification_required":
            if not allow_clarification:
                raise ValueError("Single-turn Skill authoring must return an editable draft.")
            question = _required_text(candidate, "question", MAX_QUESTION_CHARS)
            if inspect_skill_instructions(question):
                raise ValueError("Skill authoring question contains blocked safety instructions.")
            return SkillAuthoringResult(
                status="clarification_required",
                question=question,
            )
        if status != "proposal_ready":
            raise ValueError("Skill authoring result has an unsupported status.")

        resolved_name = name or _required_text(candidate, "slug", 63)
        resolved_name = validate_skill_name(resolved_name)
        resolved_description = description or _required_text(candidate, "description", MAX_DESCRIPTION_CHARS)
        fixed_template = None
        if reference_mode == "fixed-template":
            if len(references) != 1:
                raise ValueError("Reference template authoring requires exactly one document.")
            fixed_template = extract_markdown_structure(references[0].markdown)
            if not fixed_template.strip():
                raise ValueError("Reference document has no reusable Markdown structure.")
        elif authoring_mode == "regenerate":
            fixed_template = extract_fixed_reference_template(instruction)
        if fixed_template is not None and "template" not in capabilities:
            raise ValueError("Fixed templates require the template skill kind.")
        instructions = (
            build_reference_template_instructions(fixed_template)
            if fixed_template is not None
            else instruction
            if authoring_mode == "preserve"
            else _required_text(candidate, "instructions_markdown", MAX_INSTRUCTIONS_CHARS)
        )
        if len(instructions.splitlines()) > MAX_INSTRUCTIONS_LINES:
            raise ValueError(f"Skill instructions support at most {MAX_INSTRUCTIONS_LINES} lines.")
        if len(instructions) > MAX_INSTRUCTIONS_CHARS:
            raise ValueError(f"Skill instructions support at most {MAX_INSTRUCTIONS_CHARS} characters.")
        validate_allowed_tools(capabilities, allowed_tools)
        output_issues = (
            _tag_issues(inspect_skill_instructions(resolved_name), "name")
            + _tag_issues(inspect_skill_instructions(resolved_description), "description")
            + _tag_issues(inspect_skill_instructions(instructions), "instruction")
        )
        if output_issues:
            proposal = SkillAuthoringProposal(
                workspace_id=workspace_id,
                user_id=user_id,
                scope_type=scope_type,
                name=resolved_name,
                description=_redact_issues(
                    resolved_description,
                    tuple(issue for issue in output_issues if issue.source_type == "description"),
                ),
                instructions_markdown=_redact_issues(
                    instructions,
                    tuple(issue for issue in output_issues if issue.source_type == "instruction"),
                ),
                capabilities=capabilities,
                allowed_tools=allowed_tools,
            )
            return SkillAuthoringResult(
                status="blocked",
                proposal=proposal,
                issues=output_issues,
            )
        _reject_reference_literals(
            (resolved_name, resolved_description, instructions),
            tuple(reference.id for reference in references),
        )

        proposal = SkillAuthoringProposal(
            workspace_id=workspace_id,
            user_id=user_id,
            scope_type=scope_type,
            name=resolved_name,
            description=resolved_description,
            instructions_markdown=instructions,
            capabilities=capabilities,
            allowed_tools=allowed_tools,
        )
        return SkillAuthoringResult(status="proposal_ready", proposal=proposal)

    def publish(
        self,
        *,
        workspace_id: str,
        user_id: str,
        scope_type: SkillScopeType,
        name: str,
        description: str,
        instructions_markdown: str,
        expected_capabilities: tuple[SkillCapability, ...],
        expected_allowed_tools: tuple[SkillTool, ...],
    ) -> SkillAuthoringResult:
        if not expected_capabilities or any(
            capability not in CAPABILITY_TOOLS for capability in expected_capabilities
        ):
            raise ValueError("Published Skill contains an unsupported capability.")
        if len(set(expected_capabilities)) != len(expected_capabilities) or len(
            set(expected_allowed_tools)
        ) != len(expected_allowed_tools):
            raise ValueError("Published Skill permissions must not contain duplicates.")
        validate_allowed_tools(expected_capabilities, expected_allowed_tools)
        # 이미 검토를 마친 초안이므로 의도를 다시 분류하지 않는다(review_draft와 같다).
        # 재분류하면 참조 문서 없이 초안 본문만 보게 되는데, 본문에 남아 있는 고정 출력 템플릿
        # 섹션 때문에 분류기가 fixed-template이라 답하고, 참조 문서가 없는 요청에서는 그 응답이
        # 허용되지 않아 author를 통과한 초안이 publish에서 거부된다.
        reviewed = self.execute(
            workspace_id=workspace_id,
            user_id=user_id,
            scope_type=scope_type,
            name=name,
            description=description,
            instruction=instructions_markdown,
            reference_document_ids=(),
            allow_clarification=False,
            authoring_mode="preserve",
            preserved_capabilities=expected_capabilities,
            preserved_allowed_tools=expected_allowed_tools,
        )
        if reviewed.status != "proposal_ready" or reviewed.proposal is None:
            return reviewed
        # 분류를 건너뛰었으므로 검토 결과의 권한은 호출자가 확인한 권한과 같다.
        proposal = reviewed.proposal
        skill = self._skill_manager.create_published(
            workspace_id=workspace_id,
            user_id=user_id,
            scope_type=scope_type,
            slug=proposal.name,
            name=proposal.name,
            description=proposal.description,
            instructions_markdown=proposal.instructions_markdown,
            capabilities=proposal.capabilities,
            allowed_tools=proposal.allowed_tools,
        )
        return SkillAuthoringResult(status="published", proposal=proposal, skill=skill)

    def update(
        self,
        *,
        workspace_id: str,
        user_id: str,
        skill_id: str,
        name: str,
        description: str,
        instructions_markdown: str,
    ) -> SkillAuthoringResult:
        skill = self._skill_manager.get_manageable(workspace_id, user_id, skill_id)
        reviewed = self.execute(
            workspace_id=workspace_id,
            user_id=user_id,
            scope_type=skill.scope_type,
            name=name,
            description=description,
            instruction=instructions_markdown,
            reference_document_ids=(),
            allow_clarification=False,
            authoring_mode="preserve",
        )
        if reviewed.status != "proposal_ready" or reviewed.proposal is None:
            return reviewed
        proposal = reviewed.proposal
        updated = self._skill_manager.update_published(
            workspace_id=workspace_id,
            user_id=user_id,
            skill_id=skill_id,
            name=proposal.name,
            description=proposal.description,
            instructions_markdown=proposal.instructions_markdown,
            capabilities=proposal.capabilities,
            allowed_tools=proposal.allowed_tools,
        )
        return SkillAuthoringResult(status="published", proposal=proposal, skill=updated)

    def review_draft(
        self,
        *,
        workspace_id: str,
        user_id: str,
        scope_type: SkillScopeType,
        draft: SkillDraftProposal,
    ) -> SkillAuthoringResult:
        reviewed = self.execute(
            workspace_id=workspace_id,
            user_id=user_id,
            scope_type=scope_type,
            name=draft.name,
            description=draft.description,
            instruction=draft.instructions_markdown,
            reference_document_ids=(),
            allow_clarification=False,
            authoring_mode="preserve",
            preserved_capabilities=draft.capabilities,
            preserved_allowed_tools=draft.allowed_tools,
        )
        proposal = reviewed.proposal
        if reviewed.status == "proposal_ready" and proposal is not None and (
            not set(proposal.capabilities).issubset(draft.capabilities)
            or not set(proposal.allowed_tools).issubset(draft.allowed_tools)
        ):
            raise ValueError("Reviewed Skill must not expand completed AgentRun permissions.")
        return reviewed


def _validate_references(references: tuple[SkillAuthoringReference, ...]) -> tuple[SkillSafetyIssue, ...]:
    total_chars = 0
    issues: list[SkillSafetyIssue] = []
    for reference in references:
        markdown = reference.markdown
        if not markdown.strip():
            raise ValueError("Reference document must contain Markdown.")
        if len(markdown) > MAX_REFERENCE_CHARS:
            raise ValueError(f"Each reference document supports at most {MAX_REFERENCE_CHARS} characters.")
        total_chars += len(markdown)
        issues.extend(
            _tag_issues(
                inspect_skill_instructions(markdown),
                "reference",
                reference.id,
            )
        )
    if total_chars > MAX_TOTAL_REFERENCE_CHARS:
        raise ValueError(f"Reference documents support at most {MAX_TOTAL_REFERENCE_CHARS} characters in total.")
    return tuple(issues)


def _required_text(candidate: dict[str, object], key: str, max_chars: int) -> str:
    value = candidate.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Skill authoring result {key} is required.")
    normalized = value.strip()
    if len(normalized) > max_chars:
        raise ValueError(f"Skill authoring result {key} supports at most {max_chars} characters.")
    return normalized


def _llm_safety_issues(
    value: object,
    *,
    instruction: str,
    description: str | None,
    references: tuple[SkillAuthoringReference, ...],
) -> tuple[SkillSafetyIssue, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_LLM_ISSUES:
        raise ValueError("Blocked Skill authoring result must contain safety issues.")
    issues: dict[tuple[str, str | None, int, int], SkillSafetyIssue] = {}
    for value_issue in value:
        if not isinstance(value_issue, dict):
            raise ValueError("Skill authoring safety issue must be an object.")
        category = _required_text(value_issue, "category", 50)
        source_type = _required_text(value_issue, "source", 20)
        text = _required_text(value_issue, "text", 500)
        reason = _required_text(value_issue, "reason", 500)
        reference_document_id = None
        if source_type == "instruction":
            source = instruction
        elif source_type == "description" and description is not None:
            source = description
        elif source_type == "reference":
            reference_index = value_issue.get("reference_index")
            if not isinstance(reference_index, int) or isinstance(reference_index, bool):
                raise ValueError("Reference safety issue requires a reference_index.")
            if not 0 <= reference_index < len(references):
                raise ValueError("Reference safety issue index is out of range.")
            reference = references[reference_index]
            source = reference.markdown
            reference_document_id = reference.id
        else:
            raise ValueError("Skill authoring safety issue source is unsupported.")
        start = source.find(text)
        if start < 0:
            raise ValueError("Skill authoring safety issue text must exist in its source.")
        while start >= 0:
            end = start + len(text)
            issues.setdefault(
                (source_type, reference_document_id, start, end),
                SkillSafetyIssue(
                    category=category,
                    text=text,
                    reason=reason,
                    start=start,
                    end=end,
                    source_type=source_type,
                    reference_document_id=reference_document_id,
                ),
            )
            start = source.find(text, end)
    return tuple(issues.values())


def _redact_issues(source: str, issues: tuple[SkillSafetyIssue, ...]) -> str:
    ranges = sorted(
        {(issue.start, issue.end) for issue in issues if issue.start is not None and issue.end is not None}
    )
    merged_ranges: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged_ranges and start < merged_ranges[-1][1]:
            merged_ranges[-1] = (merged_ranges[-1][0], max(merged_ranges[-1][1], end))
        else:
            merged_ranges.append((start, end))
    for start, end in reversed(merged_ranges):
        source = source[:start] + "[보안상 제거됨]" + source[end:]
    return source


def _redact_sources(
    instruction: str,
    description: str | None,
    references: tuple[SkillAuthoringReference, ...],
    issues: tuple[SkillSafetyIssue, ...],
) -> tuple[str, str | None, tuple[SkillAuthoringReference, ...]]:
    instruction = _redact_issues(
        instruction,
        tuple(issue for issue in issues if issue.source_type == "instruction"),
    )
    if any(issue.source_type == "description" for issue in issues):
        description = None
    references = tuple(
        replace(
            reference,
            markdown=_redact_issues(
                reference.markdown,
                tuple(
                    issue
                    for issue in issues
                    if issue.source_type == "reference"
                    and issue.reference_document_id == reference.id
                ),
            ),
        )
        for reference in references
    )
    return instruction, description, references


def _tag_issues(
    issues: tuple[SkillSafetyIssue, ...],
    source_type: str,
    reference_document_id: str | None = None,
) -> tuple[SkillSafetyIssue, ...]:
    return tuple(
        replace(
            issue,
            source_type=source_type,
            reference_document_id=reference_document_id,
        )
        for issue in issues
    )


def _classify_intent(
    generator: SkillAuthoringGeneratorPort,
    instruction: str,
    references: tuple[SkillAuthoringReference, ...],
    description: str | None,
) -> tuple[tuple[SkillCapability, ...], str, tuple[SkillTool, ...]] | None:
    classification = _intent_result(
        generator.classify(
            instruction,
            references,
            requested_description=description,
        ),
        bool(references),
    )
    if classification[0] == "unsupported":
        raise ValueError("Skill request does not map to a supported Agent action.")
    if classification[0] != "supported":
        return None
    capabilities = classification[1]
    assert capabilities is not None
    allowed_tools = tuple(
        sorted(
            {
                tool
                for capability in capabilities
                for tool in CAPABILITY_TOOLS[capability]
            }
        )
    )
    return capabilities, classification[2], allowed_tools


def _intent_result(
    value: dict[str, object],
    has_references: bool,
) -> tuple[str, tuple[SkillCapability, ...] | None, str]:
    decision = value.get("decision")
    if decision not in {"supported", "unsupported", "ambiguous"}:
        raise ValueError("Skill intent result contains an invalid decision.")
    reference_mode = _reference_mode(value.get("reference_mode"), has_references)
    if decision != "supported":
        if value.get("skill_kinds") != []:
            raise ValueError("Unsupported or ambiguous Skill intent must not select skill kinds.")
        return cast(str, decision), None, reference_mode
    skill_kinds = value.get("skill_kinds")
    if (
        not isinstance(skill_kinds, list)
        or not skill_kinds
        or not all(isinstance(skill_kind, str) for skill_kind in skill_kinds)
        or any(skill_kind not in CAPABILITY_TOOLS for skill_kind in cast(list[str], skill_kinds))
        or len(set(cast(list[str], skill_kinds))) != len(skill_kinds)
    ):
        raise ValueError("Skill intent result requires unique supported skill_kinds.")
    capabilities = tuple(sorted(cast(list[SkillCapability], skill_kinds)))
    if reference_mode == "fixed-template" and "template" not in capabilities:
        raise ValueError("Fixed reference templates require the template skill kind.")
    return "supported", capabilities, reference_mode


def _reference_mode(value: object, has_references: bool) -> str:
    allowed = {"none", "fixed-template", "structure-reference"} if has_references else {"none"}
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("Skill authoring result contains an invalid reference mode.")
    return value


def _reject_reference_literals(values: tuple[str, ...], literals: tuple[str, ...]) -> None:
    output = "\n".join(values).casefold()
    if any(literal.strip() and literal.strip().casefold() in output for literal in literals):
        raise ValueError("Generated Skill contains a fixed reference value.")
