from dataclasses import replace
from typing import cast

from app.core.ai_markdown_sanitizer import sanitize_ai_markdown
from app.core.pipeline_control import ensure_task_active
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
    SkillResultMode,
    SkillScopeType,
    SkillTarget,
    SkillTool,
    SkillVersion,
)
from app.modules.skill.domain.exceptions import SkillRequestRejectedError
from app.modules.skill.domain.policy import (
    CAPABILITY_TOOLS,
    DOCUMENT_SKILL_CAPABILITIES,
    RESULT_MODE_CAPABILITIES,
    canonical_allowed_tools,
    can_create_document,
    effective_targets,
    is_document_skill,
    resolve_requires_source,
    validate_allowed_tools,
    validate_execution_options,
    validate_skill_name,
)
from app.modules.skill.domain.reference_template import (
    build_reference_template_instructions,
    extract_fixed_reference_template,
    extract_markdown_structure,
)
from app.modules.skill.domain.safety import (
    SkillSafetyIssue,
    inspect_skill_instructions,
    inspect_skill_output_links,
)


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
        self._skill_manager.require_manage_scope(workspace_id, user_id, scope_type)
        instruction = instruction.strip()
        max_instruction_chars = (
            MAX_INSTRUCTIONS_CHARS if authoring_mode == "preserve" else MAX_INSTRUCTION_CHARS
        )
        if not instruction or len(instruction) > max_instruction_chars:
            raise SkillRequestRejectedError(
                "invalid_instruction_length",
                f"instruction must contain 1-{max_instruction_chars} characters.",
            )
        if len(reference_document_ids) > MAX_REFERENCE_COUNT:
            raise SkillRequestRejectedError(
                "invalid_reference",
                f"reference_document_ids supports at most {MAX_REFERENCE_COUNT} documents.",
            )
        if len(set(reference_document_ids)) != len(reference_document_ids):
            raise SkillRequestRejectedError("invalid_reference", "reference_document_ids must not contain duplicates.")
        if any(not document_id.strip() for document_id in reference_document_ids):
            raise SkillRequestRejectedError("invalid_reference", "reference_document_ids must contain non-empty ids.")
        input_issues = _tag_issues(inspect_skill_instructions(instruction), "instruction")
        if input_issues and authoring_mode != "regenerate":
            return SkillAuthoringResult(status="blocked", issues=input_issues)
        if input_issues:
            instruction = _redact_issues(instruction, input_issues)
        if name is not None:
            try:
                name = validate_skill_name(name)
            except ValueError as exc:
                raise SkillRequestRejectedError("invalid_name", str(exc)) from exc
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

        classified_requires_source: bool | None = None
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
                raise SkillRequestRejectedError("intent_ambiguous", "Skill request could not be classified.")
            capabilities, reference_mode, allowed_tools, classified_requires_source = intent
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
                raise SkillRequestRejectedError("intent_ambiguous", "Skill request could not be classified.")
            capabilities, reference_mode, allowed_tools, classified_requires_source = intent
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
            # 질문은 Agent 채팅 메시지 본문에 그대로 들어간다.
            question = sanitize_ai_markdown(_required_text(candidate, "question", MAX_QUESTION_CHARS))
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
        # 초안 단계에는 결과 방식이 없다. 게시·수정 때 결과 방식과 명시값으로 다시 정한다.
        requires_source = resolve_requires_source(
            capabilities,
            None,
            instructions,
            classified=classified_requires_source,
        )
        output_issues = (
            _tag_issues(inspect_skill_instructions(resolved_name), "name")
            + _tag_issues(inspect_skill_instructions(resolved_description), "description")
            + _tag_issues(inspect_skill_instructions(instructions), "instruction")
            + _tag_issues(inspect_skill_output_links(instructions), "instruction")
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
                requires_source=requires_source,
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
            requires_source=requires_source,
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
        targets: tuple[SkillTarget, ...] | None = None,
        result_mode: SkillResultMode | None = None,
        requires_source: bool | None = None,
    ) -> SkillAuthoringResult:
        """requires_source는 사용자가 명시한 원문 필요 여부다. None이면 규칙(`{문서}`·template)으로 정하고,
        규칙으로 정할 수 없으면 False다. 게시는 의도를 다시 분류하지 않으므로(아래) 작성 초안의
        requires_source(LLM 분류 포함)를 그대로 보내야 그 판단이 유지된다.
        """
        expected_allowed_tools = _published_allowed_tools(expected_capabilities, expected_allowed_tools)
        # LLM 검토 전에 거절해 비용을 쓰지 않는다.
        targets, result_mode = validate_execution_options(expected_capabilities, targets, result_mode)
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
        proposal = replace(
            reviewed.proposal,
            requires_source=resolve_requires_source(
                expected_capabilities,
                result_mode,
                reviewed.proposal.instructions_markdown,
                explicit=requires_source,
            ),
        )
        # 호출자가 포기한 뒤에는 게시하지 않는다.
        ensure_task_active()
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
            targets=targets,
            result_mode=result_mode,
            requires_source=proposal.requires_source,
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
        targets: tuple[SkillTarget, ...] | None = None,
        result_mode: SkillResultMode | None = None,
        requires_source: bool | None = None,
        capabilities: tuple[SkillCapability, ...] | None = None,
    ) -> SkillAuthoringResult:
        """수정은 보낸 필드만 바꾸는 부분 수정이다. targets·result_mode·requires_source는 None이면 기존 값을 유지한다.
        targets=()는 기본값으로 되돌린다.

        지침을 고쳤어도 의도를 다시 분류하지 않고 이전 capabilities·allowed_tools를 그대로 쓴다.
        실행 규칙 문장만 고친 수정이 분류기 판단에 따라 Skill 종류(예: 폴더 정리 -> 문서 수정+폴더 정리)와
        대상·결과 방식을 바꾸면 이후 실행이 엉뚱한 대상을 요구하기 때문이다. 의도 분류는 작성(초안) 경로에서만 한다.
        지침이 바뀌면 안전 검토(LLM)는 그대로 다시 한다.

        capabilities를 보내면 사용자가 Skill 종류·결과 방식을 직접 바꾼 것이다(잘못 저장된 종류의 복구 포함).
        게시와 같은 규칙으로 검증하고 allowed_tools를 다시 계산한다. 생략한 result_mode는 이전 값 대신
        capabilities에서 정해지는 값을 쓴다(_updated_execution_options).
        """
        skill = self._skill_manager.get_manageable(workspace_id, user_id, skill_id)
        previous = skill.latest_version or skill.enabled_version
        preserved_capabilities = None
        preserved_allowed_tools = None
        if capabilities is not None:
            # LLM 검토 전에 거절해 비용을 쓰지 않는다(게시와 같다).
            preserved_allowed_tools = _published_allowed_tools(capabilities, ())
            preserved_capabilities = capabilities
            validate_execution_options(
                capabilities, targets, result_mode or _capability_result_mode(capabilities)
            )
        elif previous is not None:
            preserved_capabilities = previous.capabilities
            # 컬럼 추가 전 행처럼 저장된 Tool 표가 비어 있으면 게시와 같이 서버 정책으로 계산한다.
            preserved_allowed_tools = previous.allowed_tools or canonical_allowed_tools(previous.capabilities)
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
            preserved_capabilities=preserved_capabilities,
            preserved_allowed_tools=preserved_allowed_tools,
        )
        if reviewed.status != "proposal_ready" or reviewed.proposal is None:
            return reviewed
        proposal = reviewed.proposal
        targets, result_mode = _updated_execution_options(
            previous,
            proposal.capabilities,
            targets,
            result_mode,
            capabilities_changed=capabilities is not None,
        )
        proposal = replace(
            proposal,
            requires_source=_updated_requires_source(
                previous,
                proposal,
                result_mode,
                requires_source,
                preserved=preserved_capabilities is not None,
            ),
        )
        ensure_task_active()
        updated = self._skill_manager.update_published(
            workspace_id=workspace_id,
            user_id=user_id,
            skill_id=skill_id,
            name=proposal.name,
            description=proposal.description,
            instructions_markdown=proposal.instructions_markdown,
            capabilities=proposal.capabilities,
            allowed_tools=proposal.allowed_tools,
            targets=targets,
            result_mode=result_mode,
            requires_source=proposal.requires_source,
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


def _published_allowed_tools(
    capabilities: tuple[SkillCapability, ...],
    allowed_tools: tuple[SkillTool, ...],
) -> tuple[SkillTool, ...]:
    """게시·수정에서 사용자가 정한 capabilities를 검증하고 저장할 Tool 표를 돌려준다."""
    if not capabilities or any(capability not in CAPABILITY_TOOLS for capability in capabilities):
        raise ValueError("Published Skill contains an unsupported capability.")
    if not allowed_tools:
        # 초안 없이 바로 게시·수정하는 화면은 Tool 표를 모른다. 서버 정책으로 계산해 아래 검증을 그대로 거친다.
        allowed_tools = canonical_allowed_tools(capabilities)
    if len(set(capabilities)) != len(capabilities) or len(set(allowed_tools)) != len(allowed_tools):
        raise ValueError("Published Skill permissions must not contain duplicates.")
    validate_allowed_tools(capabilities, allowed_tools)
    return allowed_tools


def _capability_result_mode(capabilities: tuple[SkillCapability, ...]) -> SkillResultMode | None:
    """capabilities의 문서 기능 조합으로 정해지는 결과 방식(edit/create/auto). 문서 Skill이 아니면 None."""
    document_capabilities = DOCUMENT_SKILL_CAPABILITIES.intersection(capabilities)
    for mode, required in RESULT_MODE_CAPABILITIES.items():
        if required == document_capabilities:
            return mode
    return None


def _updated_execution_options(
    previous: SkillVersion | None,
    capabilities: tuple[SkillCapability, ...],
    targets: tuple[SkillTarget, ...] | None,
    result_mode: SkillResultMode | None,
    *,
    capabilities_changed: bool = False,
) -> tuple[tuple[SkillTarget, ...], SkillResultMode | None]:
    """수정 버전의 targets·result_mode. 보내지 않은 값은 이전 버전에서 이어받는다.

    capabilities를 이어받았으면 이어받은 값은 capabilities와 맞는다.
    capabilities를 새로 보냈으면(capabilities_changed) 생략한 result_mode는 이전 값 대신 capabilities에서 정한다.
    targets는 이전 버전도 문서 Skill이었을 때만 이어받고, 아니면 기본값(["current_document"])이다.
    보낸 값이 capabilities와 어긋나면 invalid_result_mode·invalid_targets로 거절한다.
    문서 Skill이 아니면 targets·result_mode가 의미가 없으므로 이어받지 않는다.
    """
    if targets is None:
        targets = (
            effective_targets(previous.capabilities, previous.targets)
            if previous is not None and is_document_skill(capabilities)
            else ()
        )
    if result_mode is None and is_document_skill(capabilities):
        if capabilities_changed:
            result_mode = _capability_result_mode(capabilities)
        elif previous is not None:
            result_mode = previous.result_mode
    return validate_execution_options(capabilities, targets, result_mode)


def _updated_requires_source(
    previous: SkillVersion | None,
    proposal: SkillAuthoringProposal,
    result_mode: SkillResultMode | None,
    explicit: bool | None,
    *,
    preserved: bool,
) -> bool:
    """수정 버전의 원문 필요 여부. 명시값이 없으면 지침을 고쳤어도 이전 생성 Skill의 값을 이어받는다.

    이전 버전이 새 문서를 만들지 않던 Skill이면(예: 결과 방식 edit -> create) 이어받을 값이 없어 규칙으로 정한다.
    preserved는 이전 버전의 capabilities를 이어받았는지다(이전 버전이 없을 때만 False).
    """
    if (
        explicit is None
        and preserved
        and previous is not None
        and can_create_document(previous.capabilities, previous.result_mode)
    ):
        explicit = previous.requires_source
    return resolve_requires_source(
        proposal.capabilities,
        result_mode,
        proposal.instructions_markdown,
        explicit=explicit,
        classified=None if preserved else proposal.requires_source,
    )


def _validate_references(references: tuple[SkillAuthoringReference, ...]) -> tuple[SkillSafetyIssue, ...]:
    total_chars = 0
    issues: list[SkillSafetyIssue] = []
    for reference in references:
        markdown = reference.markdown
        if not markdown.strip():
            raise SkillRequestRejectedError("invalid_reference", "Reference document must contain Markdown.")
        if len(markdown) > MAX_REFERENCE_CHARS:
            raise SkillRequestRejectedError(
                "invalid_reference",
                f"Each reference document supports at most {MAX_REFERENCE_CHARS} characters.",
            )
        total_chars += len(markdown)
        issues.extend(
            _tag_issues(
                inspect_skill_instructions(markdown),
                "reference",
                reference.id,
            )
        )
    if total_chars > MAX_TOTAL_REFERENCE_CHARS:
        raise SkillRequestRejectedError(
            "invalid_reference",
            f"Reference documents support at most {MAX_TOTAL_REFERENCE_CHARS} characters in total.",
        )
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
) -> tuple[tuple[SkillCapability, ...], str, tuple[SkillTool, ...], bool | None] | None:
    raw_classification = generator.classify(
        instruction,
        references,
        requested_description=description,
    )
    classification = _intent_result(raw_classification, bool(references))
    if classification[0] == "unsupported":
        raise SkillRequestRejectedError(
            "intent_unsupported",
            "Skill request does not map to a supported Agent action.",
        )
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
    return capabilities, classification[2], allowed_tools, _classified_requires_source(raw_classification)


def _classified_requires_source(value: dict[str, object]) -> bool | None:
    """분류기의 원문 필요 판단. 이전 프롬프트처럼 값이 없거나 bool이 아니면 None(규칙·False로 정한다)."""
    requires_source = value.get("requires_source")
    return requires_source if isinstance(requires_source, bool) else None


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
