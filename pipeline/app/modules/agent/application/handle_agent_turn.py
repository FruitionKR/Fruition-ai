import hashlib
import re
from collections.abc import Callable
from dataclasses import replace

from app.modules.agent.application.ports import (
    AgentTurnRouterPort,
    ConversationReplierPort,
)
from app.modules.agent.domain.entities import (
    AgentAction,
    AgentConversationContext,
    AgentTurnRequest,
    AgentTurnResult,
    AgentTurnRoute,
    PendingSkillProposal,
    RetrievalSource,
)
from app.modules.agent.domain.exceptions import AgentConfigurationError
from app.modules.agent_run.application.ports import (
    AgentRunManagementRepositoryPort,
    AgentRunStarterPort,
)
from app.modules.agent_run.domain.entities import (
    StartAgentRunContent,
    StartAgentRunRequest,
)
from app.modules.markdown_edit.application.generate_markdown_document import (
    GenerateMarkdownDocumentUseCase,
)
from app.modules.markdown_edit.application.generate_markdown_edit import (
    GenerateMarkdownEditUseCase,
)
from app.modules.markdown_edit.domain.entities import (
    MarkdownCreateRequest,
    MarkdownEditOperation,
    MarkdownEditRequest,
    MarkdownEditTarget,
)
from app.modules.markdown_edit.domain.markdown_target_scope import (
    apply_markdown_edit,
    markdown_line_count,
    markdown_line_range,
)
from app.modules.query.application.answer_query import AnswerQueryUseCase
from app.modules.query.application.conversation_context_resolver import (
    conversation_messages_text,
    update_conversation_summary,
)
from app.modules.query.application.ports import ConversationSummarizerPort
from app.modules.query.domain.entities import ConversationContext, QueryAnswer
from app.modules.skill.application.author_skill import AuthorSkillUseCase
from app.modules.skill.application.propose_skill_draft import ProposeSkillDraftUseCase
from app.modules.skill.application.select_skill import (
    PreparedSkillSelection,
    SelectSkillUseCase,
)
from app.modules.skill.domain.entities import (
    Skill,
    SkillAuthoringProposal,
    SkillAuthoringResult,
    SkillScopeType,
)
from app.modules.skill.domain.policy import validate_skill_name
from app.modules.skill.domain.safety import inspect_skill_instructions

CLARIFY_MARKDOWN_TARGET_MESSAGE = "수정할 Markdown 범위를 선택한 뒤 다시 요청해 주세요."
CLARIFY_MARKDOWN_DOCUMENT_MESSAGE = "수정할 Markdown 문서를 연 뒤 다시 요청해 주세요."
DEFERRED_TEMPLATE_MESSAGE = "template 기반 전체 문서 재구성은 이후 단계에서 다루겠습니다. 현재는 선택 영역, 현재 섹션, 또는 문서 전체의 일반 편집만 지원합니다."
CLARIFY_INSERT_AFTER_TARGET_MESSAGE = "내용을 추가할 현재 섹션을 선택한 뒤 다시 요청해 주세요."
CLARIFY_SKILL_MESSAGE = "이 요청에 적용할 Skill을 선택하거나 Skill 없이 계속해 주세요."
CLARIFY_INCOMPATIBLE_SKILL_MESSAGE = (
    "선택한 Skill이 요청한 모든 작업을 지원하지 않습니다. "
    "다른 Skill을 선택하거나 요청 범위를 줄여 주세요."
)
CLARIFY_PREVIEW_MESSAGE = "저장할 이전 미리보기를 확인할 수 없어 미리보기를 다시 만들어 주세요."
NO_CHANGES_MESSAGE = "원문에서 변경할 내용이 없어 저장 작업을 만들지 않았습니다."
BLOCKED_SKILL_AUTHORING_MESSAGE = "보안 문제가 있는 내용을 제거하거나 수정한 뒤 다시 시도해 주세요."
TITLE_REVISION_PATTERN = re.compile(
    r"(?:제목|이름|커맨드|식별자)(?:을|를)?\s*(?:[\"“「](.+?)[\"”」]|(.+?))\s*(?:로|으로)\s*(?:바꿔|변경|수정)"
)
PUBLISH_SKILL_PATTERN = re.compile(
    r"(?:이대로\s*)?(?:게시|등록)(?:해|해줘|해주세요|하자)|"
    r"(?:please\s+)?(?:publish|post)(?:\s+(?:it|this|the\s+skill))?",
    re.IGNORECASE,
)
SECURITY_REVIEW_PATTERN = re.compile(r"보안\s*(?:재)?검토|다시\s*검증|security\s*(?:re)?view", re.IGNORECASE)
REGENERATE_PATTERN = re.compile(r"(?:AI로\s*)?재생성|다시\s*(?:만들어|작성)|regenerate", re.IGNORECASE)


class HandleAgentTurnUseCase:
    def __init__(
        self,
        router: AgentTurnRouterPort,
        query_use_case: AnswerQueryUseCase,
        markdown_edit_use_case: GenerateMarkdownEditUseCase,
        markdown_create_use_case: GenerateMarkdownDocumentUseCase,
        skill_selector: SelectSkillUseCase | None = None,
        agent_run_starter: AgentRunStarterPort | None = None,
        skill_authorer: AuthorSkillUseCase | None = None,
        skill_draft_proposer: ProposeSkillDraftUseCase | None = None,
        conversation_summarizer: ConversationSummarizerPort | None = None,
        web_search_query_use_case_factory: Callable[[], AnswerQueryUseCase] | None = None,
        conversation_replier: ConversationReplierPort | None = None,
        markdown_turn_repository: AgentRunManagementRepositoryPort | None = None,
    ) -> None:
        self._router = router
        self._query_use_case = query_use_case
        self._markdown_edit_use_case = markdown_edit_use_case
        self._markdown_create_use_case = markdown_create_use_case
        self._skill_selector = skill_selector
        self._agent_run_starter = agent_run_starter
        self._skill_authorer = skill_authorer
        self._skill_draft_proposer = skill_draft_proposer
        self._conversation_summarizer = conversation_summarizer
        self._web_search_query_use_case_factory = web_search_query_use_case_factory
        self._conversation_replier = conversation_replier
        self._markdown_turn_repository = markdown_turn_repository

    def execute(self, request: AgentTurnRequest) -> AgentTurnResult:
        if not request.message.strip():
            raise ValueError("message is required.")

        result = self._execute(request)
        updated_summary = (
            result.query_answer.updated_conversation_summary
            if result.query_answer is not None
            else update_conversation_summary(
                _to_query_conversation_context(request),
                self._conversation_summarizer,
            )
        )
        return replace(result, updated_conversation_summary=updated_summary)

    def _execute(self, request: AgentTurnRequest) -> AgentTurnResult:
        selection = self._prepare_skill_selection(request)
        request = selection.request
        resolved = selection.resolve_route(self._router.route(request))
        route = resolved.route
        selected_skill = resolved.skill
        if selection.explicit_skill_id is not None and selected_skill is None:
            return AgentTurnResult(
                action="clarify",
                route=replace(
                    route,
                    action="clarify",
                    confidence=0.0,
                    reason="The selected Skill does not cover every required capability.",
                    edit_goal=None,
                    edit_operation=None,
                    edit_destination=None,
                    selected_skill_id=None,
                    skill_candidates=(),
                    retrieval_source="none",
                    document_operation="none",
                    persist=False,
                    required_capabilities=(),
                ),
                message=CLARIFY_INCOMPATIBLE_SKILL_MESSAGE,
            )
        if route.action == "clarify" and route.skill_candidates:
            candidates = tuple(
                candidate
                for candidate in request.available_skills
                if candidate.id in route.skill_candidates
            )
            return AgentTurnResult(
                action="clarify",
                route=route,
                message=CLARIFY_SKILL_MESSAGE,
                skill_candidates=candidates,
            )
        if route.action == "markdown_create":
            return self._execute_markdown_create(request, route, selected_skill)

        if route.action == "skill_draft_proposal":
            if not request.skill_draft_sources:
                return AgentTurnResult(
                    action="clarify",
                    route=route,
                    message="Skill로 만들 완료 작업을 선택해 주세요.",
                )
            if (
                self._skill_draft_proposer is None
                or self._skill_authorer is None
                or not request.workspace_id
                or not request.user_id
            ):
                raise AgentConfigurationError("Skill draft proposal is not configured.")
            scope_type = _skill_scope_type(request)
            if scope_type is None:
                return AgentTurnResult(
                    action="clarify",
                    route=route,
                    message="개인 스킬로 만들까요, 현재 팀 스킬로 만들까요?",
                )
            proposal = self._skill_draft_proposer.execute(
                source_runs=request.skill_draft_sources,
                user_directives=request.skill_draft_user_directives,
                excluded_literals=request.skill_draft_excluded_literals,
            )
            reviewed = self._skill_authorer.review_draft(
                workspace_id=request.workspace_id,
                user_id=request.user_id,
                scope_type=scope_type,
                draft=proposal,
            )
            return AgentTurnResult(
                action="skill_authoring",
                route=route,
                message=(
                    BLOCKED_SKILL_AUTHORING_MESSAGE
                    if reviewed.status == "blocked"
                    else _skill_authoring_message(reviewed)
                ),
                skill_authoring_result=reviewed,
            )

        if route.action == "skill_authoring":
            if not request.workspace_id or not request.user_id:
                raise AgentConfigurationError("Skill authoring requires workspace_id and user_id.")
            pending_proposal = (
                request.conversation_context.pending_skill_proposal
                if request.conversation_context
                else None
            )
            if pending_proposal is not None:
                authored = self._handle_pending_skill(request, pending_proposal)
            else:
                if self._skill_authorer is None:
                    raise AgentConfigurationError("Skill authoring is not configured.")
                scope_type = _skill_scope_type(request)
                if scope_type is None:
                    authored = SkillAuthoringResult(
                        status="clarification_required",
                        question="개인 스킬로 만들까요, 현재 팀 스킬로 만들까요?",
                    )
                else:
                    authored = self._skill_authorer.execute(
                        workspace_id=request.workspace_id,
                        user_id=request.user_id,
                        scope_type=scope_type,
                        instruction=_skill_authoring_instruction(request),
                        reference_document_ids=request.skill_reference_document_ids,
                        allow_clarification=True,
                        authoring_mode=request.skill_authoring_mode,
                    )
            message = (
                BLOCKED_SKILL_AUTHORING_MESSAGE
                if authored.status == "blocked"
                else authored.question or _skill_authoring_message(authored)
            )
            return AgentTurnResult(
                action="skill_authoring",
                route=route,
                message=message,
                skill_authoring_result=authored,
            )

        if route.action in {"folder_organize", "workspace_workflow"}:
            if self._agent_run_starter is None or not request.workspace_id or not request.user_id:
                raise AgentConfigurationError("Workspace workflow requires workspace_id and user_id.")
            if inspect_skill_instructions(request.message):
                return _reject_unsafe_workspace_mutation(route)
            preview = _latest_markdown_preview(request)
            preview_action, preview_run_id = preview or (None, None)
            reuses_edit_preview = (
                route.action == "workspace_workflow"
                and route.document_operation == "edit"
                and preview_action == "markdown_edit"
                and preview_run_id is not None
            )
            reuses_create_preview = (
                route.action == "workspace_workflow"
                and route.document_operation == "create"
                and preview_action == "markdown_create"
                and preview_run_id is not None
            )
            skill_version_id = (
                selected_skill.enabled_version.id
                if selected_skill is not None and selected_skill.enabled_version is not None
                else None
            )
            content = None
            if route.action == "workspace_workflow" and route.document_operation == "create":
                creation_markdown = (
                    self._confirmed_preview_markdown(request, preview_run_id)
                    if reuses_create_preview
                    else None
                )
                if reuses_create_preview and creation_markdown is None:
                    return _clarify_document_change(route, CLARIFY_PREVIEW_MESSAGE)
                if creation_markdown is None:
                    reference_context = self._resolve_reference_context(request, route)
                    creation_markdown = self._markdown_create_use_case.execute(
                        MarkdownCreateRequest(
                            instruction=request.message,
                            workspace_id=request.workspace_id,
                            user_id=request.user_id,
                            conversation_summary=_conversation_context_text(request),
                            reference_context=reference_context,
                            skill_instructions=_skill_instructions(selected_skill),
                            output_language=request.output_language,
                        )
                    ).document.markdown
                content = StartAgentRunContent(markdown=creation_markdown)
            elif route.action == "workspace_workflow" and route.document_operation == "edit":
                markdown_context = request.active_markdown_context
                if (
                    markdown_context is None
                    or not markdown_context.markdown.strip()
                    or request.document_id is None
                    or request.base_version is None
                ):
                    return AgentTurnResult(
                        action="clarify",
                        route=replace(
                            route,
                            action="clarify",
                            retrieval_source="none",
                            persist=False,
                        ),
                        message=CLARIFY_MARKDOWN_DOCUMENT_MESSAGE,
                    )
                target = markdown_context.target or _whole_document_target(markdown_context.markdown)
                if _missing_current_section(route, target):
                    return _clarify_document_change(
                        route,
                        CLARIFY_INSERT_AFTER_TARGET_MESSAGE,
                    )
                edit = (
                    self._confirmed_preview_edit(request, preview_run_id)
                    if reuses_edit_preview
                    else None
                )
                if reuses_edit_preview and edit is None:
                    return _clarify_document_change(route, CLARIFY_PREVIEW_MESSAGE)
                if edit is None:
                    reference_context = self._resolve_reference_context(request, route)
                    edit = self._markdown_edit_use_case.execute(
                        MarkdownEditRequest(
                            instruction=request.message,
                            markdown=markdown_context.markdown,
                            target=target,
                            workspace_id=request.workspace_id,
                            user_id=request.user_id,
                            conversation_summary=_conversation_context_text(request),
                            reference_context=reference_context,
                            edit_goal=route.edit_goal,
                            edit_operation=route.edit_operation or "replace",
                            edit_destination=route.edit_destination or "target",
                            skill_instructions=_skill_instructions(selected_skill),
                            output_language=request.output_language,
                        )
                    ).edit
                route = _resolved_edit_route(route, edit)
                edited_markdown = apply_markdown_edit(markdown_context.markdown, edit)
                if not edit.changed or edited_markdown == markdown_context.markdown:
                    return _clarify_document_change(route, NO_CHANGES_MESSAGE)
                actual_target = edit.actual_target
                content = StartAgentRunContent(
                    markdown=edited_markdown,
                    purpose="apply_document_edit",
                    document_id=request.document_id,
                    base_version=request.base_version,
                    target={
                        "type": actual_target.type,
                        "start_line": actual_target.start_line,
                        "end_line": actual_target.end_line,
                    },
                )
            run_id, run_status = self._agent_run_starter.start(
                StartAgentRunRequest(
                    workspace_id=request.workspace_id,
                    user_id=request.user_id,
                    instruction=request.message,
                    provider=request.provider,
                    model=request.model,
                    action=route.action,
                    skill_version_id=skill_version_id,
                    content=content,
                )
            )
            return AgentTurnResult(
                action=route.action,
                route=route,
                run_id=run_id,
                run_status=run_status,
            )

        if route.action == "markdown_edit":
            return self._execute_markdown_edit(request, route, selected_skill)

        if route.action == "clarify":
            if route.edit_goal == "template_transform":
                message = DEFERRED_TEMPLATE_MESSAGE
            elif route.edit_operation == "insert_after":
                message = CLARIFY_INSERT_AFTER_TARGET_MESSAGE
            else:
                message = CLARIFY_MARKDOWN_TARGET_MESSAGE
            return AgentTurnResult(action="clarify", route=route, message=message)

        if route.action == "reject":
            return AgentTurnResult(
                action="reject",
                route=route,
                message="요청한 작업은 현재 지원 범위에서 처리할 수 없습니다.",
            )

        if route.action == "conversation_reply":
            return self._execute_conversation_reply(request, route)

        return self._execute_chat_answer(request, route)

    def _confirmed_preview_edit(
        self,
        request: AgentTurnRequest,
        run_id: str | None,
    ) -> MarkdownEditOperation | None:
        result = self._completed_preview_result(request, run_id, match_document=True)
        markdown_context = request.active_markdown_context
        if result is None or markdown_context is None:
            return None
        return _preview_edit(result, markdown_context.markdown)

    def _confirmed_preview_markdown(
        self,
        request: AgentTurnRequest,
        run_id: str | None,
    ) -> str | None:
        result = self._completed_preview_result(request, run_id, match_document=False)
        return _preview_created_markdown(result)

    def _completed_preview_result(
        self,
        request: AgentTurnRequest,
        run_id: str | None,
        *,
        match_document: bool,
    ) -> object | None:
        if (
            self._markdown_turn_repository is None
            or run_id is None
            or request.workspace_id is None
            or request.user_id is None
        ):
            return None
        status = self._markdown_turn_repository.get_markdown_turn_status(
            request.workspace_id,
            request.user_id,
            run_id,
        )
        if (
            not status
            or status.get("status") != "completed"
            or (
                match_document
                and (
                    status.get("document_id") != request.document_id
                    or status.get("base_version") != request.base_version
                )
            )
        ):
            return None
        return status.get("result")

    def _answer_query(
        self,
        request: AgentTurnRequest,
        retrieval_source: str,
    ) -> QueryAnswer:
        allow_web_search = retrieval_source == "web"
        query_kwargs: dict[str, object] = {
            "workspace_id": request.workspace_id or "",
            "user_id": request.user_id,
            "conversation_context": _to_query_conversation_context(request),
        }
        if request.output_language is not None:
            query_kwargs["output_language"] = request.output_language
        if request.response_length is not None:
            query_kwargs["response_length"] = request.response_length
        query_kwargs["allow_web_search"] = allow_web_search
        query_use_case = (
            self._web_search_query_use_case_factory()
            if allow_web_search is True and self._web_search_query_use_case_factory is not None
            else self._query_use_case
        )
        return query_use_case.execute(request.message, **query_kwargs)

    def _execute_chat_answer(
        self,
        request: AgentTurnRequest,
        route: AgentTurnRoute,
    ) -> AgentTurnResult:
        return AgentTurnResult(
            action="chat_answer",
            route=route,
            query_answer=self._answer_query(
                _with_active_markdown_reference(request),
                route.retrieval_source,
            ),
        )

    def _execute_conversation_reply(
        self,
        request: AgentTurnRequest,
        route: AgentTurnRoute,
    ) -> AgentTurnResult:
        if self._conversation_replier is None:
            raise AgentConfigurationError("Conversation reply is not configured.")
        message = self._conversation_replier.reply(request)
        return AgentTurnResult(
            action="conversation_reply",
            route=_non_mutating_route(
                route,
                action="conversation_reply",
                reason=route.reason,
            ),
            message=message,
        )

    def _execute_markdown_edit(
        self,
        request: AgentTurnRequest,
        route: AgentTurnRoute,
        selected_skill: Skill | None,
    ) -> AgentTurnResult:
        markdown_context = request.active_markdown_context
        if markdown_context is None or not markdown_context.markdown.strip():
            return _clarify_document_change(route, CLARIFY_MARKDOWN_DOCUMENT_MESSAGE)
        target = markdown_context.target or _whole_document_target(markdown_context.markdown)
        if _missing_current_section(route, target):
            return _clarify_document_change(
                route,
                CLARIFY_INSERT_AFTER_TARGET_MESSAGE,
            )
        reference_context = self._resolve_reference_context(request, route)
        result = self._markdown_edit_use_case.execute(
            MarkdownEditRequest(
                instruction=request.message,
                markdown=markdown_context.markdown,
                target=target,
                workspace_id=request.workspace_id,
                user_id=request.user_id,
                conversation_summary=_conversation_context_text(request),
                reference_context=reference_context,
                edit_goal=route.edit_goal,
                edit_operation=route.edit_operation or "replace",
                edit_destination=route.edit_destination or "target",
                skill_instructions=_skill_instructions(selected_skill),
                output_language=request.output_language,
            )
        )
        resolved_route = _resolved_edit_route(route, result.edit)
        return AgentTurnResult(
            action="markdown_edit",
            route=resolved_route,
            edit=result.edit,
            source_markdown_sha256=_markdown_sha256(markdown_context.markdown),
        )

    def _execute_markdown_create(
        self,
        request: AgentTurnRequest,
        route: AgentTurnRoute,
        selected_skill: Skill | None,
    ) -> AgentTurnResult:
        reference_context = self._resolve_reference_context(request, route)
        result = self._markdown_create_use_case.execute(
            MarkdownCreateRequest(
                instruction=request.message,
                workspace_id=request.workspace_id,
                user_id=request.user_id,
                conversation_summary=_conversation_context_text(request),
                reference_context=reference_context,
                skill_instructions=_skill_instructions(selected_skill),
                output_language=request.output_language,
            )
        )
        return AgentTurnResult(
            action="markdown_create",
            route=route,
            generated_markdown=result.document,
        )

    def _resolve_reference_context(
        self,
        request: AgentTurnRequest,
        route: AgentTurnRoute,
    ) -> dict[str, object]:
        reference_context = dict(
            request.conversation_context.reference_context
            if request.conversation_context
            else {}
        )
        if route.retrieval_source == "none":
            return reference_context
        grounded_answer = self._answer_query(request, route.retrieval_source)
        # 근거 id(source_document_id·source_block_ids)는 담지 않는다. 이 dict는 LLM payload와
        # 질의 참조 라인으로만 쓰이는데, id를 보여주면 LLM이 [doc:B0001] 형태를 답변에 만들어낸다.
        # citation 매핑은 별도 references 배열로 전달된다.
        reference_context["grounded_query"] = {
            "answer": grounded_answer.answer.content,
            "evidence_snippets": [
                {
                    "rank": snippet.rank,
                    "text": snippet.text,
                }
                for snippet in grounded_answer.evidence_snippets
            ],
        }
        return reference_context

    def _handle_pending_skill(
        self,
        request: AgentTurnRequest,
        proposal: PendingSkillProposal,
    ) -> SkillAuthoringResult:
        if self._skill_authorer is None:
            raise AgentConfigurationError("Skill authoring is not configured.")
        if PUBLISH_SKILL_PATTERN.fullmatch(request.message.strip()):
            return self._skill_authorer.publish(
                workspace_id=request.workspace_id or "",
                user_id=request.user_id or "",
                scope_type=proposal.scope_type,
                name=proposal.name,
                description=proposal.description,
                instructions_markdown=proposal.instructions_markdown,
                expected_capabilities=proposal.capabilities,
                expected_allowed_tools=proposal.allowed_tools,
            )
        if SECURITY_REVIEW_PATTERN.search(request.message):
            return self._skill_authorer.execute(
                workspace_id=request.workspace_id or "",
                user_id=request.user_id or "",
                scope_type=proposal.scope_type,
                name=proposal.name,
                description=proposal.description,
                instruction=proposal.instructions_markdown,
                reference_document_ids=(),
                allow_clarification=False,
                authoring_mode="preserve",
            )
        if REGENERATE_PATTERN.search(request.message):
            return self._skill_authorer.execute(
                workspace_id=request.workspace_id or "",
                user_id=request.user_id or "",
                scope_type=proposal.scope_type,
                name=proposal.name,
                instruction=proposal.instructions_markdown,
                reference_document_ids=(),
                allow_clarification=False,
                authoring_mode="regenerate",
            )
        match = TITLE_REVISION_PATTERN.search(request.message.strip())
        if match is not None:
            name = validate_skill_name(match.group(1) or match.group(2) or "")
            issues = inspect_skill_instructions(name)
            if issues:
                return SkillAuthoringResult(status="blocked", issues=issues)
            return SkillAuthoringResult(
                status="proposal_ready",
                proposal=SkillAuthoringProposal(
                    workspace_id=request.workspace_id or "",
                    user_id=request.user_id or "",
                    scope_type=proposal.scope_type,
                    name=name,
                    description=proposal.description,
                    instructions_markdown=proposal.instructions_markdown,
                    capabilities=proposal.capabilities,
                    allowed_tools=proposal.allowed_tools,
                ),
            )
        scope_type = _scope_from_text(request.message)
        if scope_type is not None:
            return SkillAuthoringResult(
                status="proposal_ready",
                proposal=SkillAuthoringProposal(
                    workspace_id=request.workspace_id or "",
                    user_id=request.user_id or "",
                    scope_type=scope_type,
                    name=proposal.name,
                    description=proposal.description,
                    instructions_markdown=proposal.instructions_markdown,
                    capabilities=proposal.capabilities,
                    allowed_tools=proposal.allowed_tools,
                ),
            )
        raise ValueError("현재 제안은 게시, 재생성, 보안 재검토, 커맨드·범위 변경을 지원합니다.")

    def _prepare_skill_selection(self, request: AgentTurnRequest) -> PreparedSkillSelection:
        if self._skill_selector is not None:
            return self._skill_selector.prepare(request)
        return PreparedSkillSelection(request=request, skills=())


def _resolved_edit_route(
    route: AgentTurnRoute,
    edit: MarkdownEditOperation,
) -> AgentTurnRoute:
    return replace(
        route,
        edit_operation=edit.operation,
        edit_destination=(
            "document_end"
            if edit.operation == "insert_after" and edit.actual_target.type == "whole_document"
            else "target"
        ),
    )


def _non_mutating_route(
    route: AgentTurnRoute,
    *,
    action: AgentAction,
    reason: str,
    retrieval_source: RetrievalSource = "none",
) -> AgentTurnRoute:
    return replace(
        route,
        action=action,
        confidence=1.0,
        reason=reason,
        edit_goal=None,
        edit_operation=None,
        edit_destination=None,
        selected_skill_id=None,
        skill_candidates=(),
        retrieval_source=retrieval_source,
        document_operation="none",
        persist=False,
        required_capabilities=(),
    )


def _latest_markdown_preview(request: AgentTurnRequest) -> tuple[str, str] | None:
    if request.conversation_context is None:
        return None
    for message in reversed(request.conversation_context.recent_messages):
        if message.role == "assistant":
            if message.action in {"markdown_create", "markdown_edit"} and message.run_id:
                return message.action, message.run_id
            return None
    return None


def _preview_edit(value: object, markdown: str) -> MarkdownEditOperation | None:
    if not isinstance(value, dict) or value.get("action") != "markdown_edit":
        return None
    if value.get("source_markdown_sha256") != _markdown_sha256(markdown):
        return None
    edit = value.get("edit")
    if not isinstance(edit, dict) or edit.get("changed") is not True:
        return None
    actual_target = edit.get("actual_target")
    if not isinstance(actual_target, dict):
        return None
    target_type = actual_target.get("type")
    start_line = actual_target.get("start_line")
    end_line = actual_target.get("end_line")
    if (
        target_type not in {"selection", "current_section", "whole_document"}
        or not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not isinstance(end_line, int)
        or isinstance(end_line, bool)
        or start_line < 1
        or end_line < start_line
        or end_line > markdown_line_count(markdown)
    ):
        return None
    operation = edit.get("operation")
    summary = edit.get("summary")
    replacement_markdown = edit.get("replacement_markdown")
    if (
        operation not in {"replace", "insert_after"}
        or not isinstance(summary, str)
        or not isinstance(replacement_markdown, str)
        or not replacement_markdown.strip()
    ):
        return None
    target = MarkdownEditTarget(type=target_type, start_line=start_line, end_line=end_line)
    return MarkdownEditOperation(
        operation=operation,
        target=target,
        requested_target=target,
        summary=summary,
        replacement_markdown=replacement_markdown,
    )


def _preview_created_markdown(value: object) -> str | None:
    if not isinstance(value, dict) or value.get("action") != "markdown_create":
        return None
    generated = value.get("generated_markdown")
    if not isinstance(generated, dict):
        return None
    markdown = generated.get("markdown")
    return markdown if isinstance(markdown, str) and markdown.strip() else None


def _markdown_sha256(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _clarify_document_change(route: AgentTurnRoute, message: str) -> AgentTurnResult:
    keeps_edit_context = route.document_operation == "edit"
    return AgentTurnResult(
        action="clarify",
        route=replace(
            route,
            action="clarify",
            confidence=0.0,
            reason=message,
            retrieval_source="none",
            document_operation="edit" if keeps_edit_context else "none",
            edit_goal=route.edit_goal if keeps_edit_context else None,
            edit_operation=route.edit_operation if keeps_edit_context else None,
            edit_destination=route.edit_destination if keeps_edit_context else None,
            persist=False,
        ),
        message=message,
    )


def _to_query_conversation_context(request: AgentTurnRequest) -> ConversationContext | None:
    if request.conversation_context is None:
        return None
    if (
        request.conversation_context.recent_conversation_summary is None
        and not request.conversation_context.recent_messages
        and not request.conversation_context.reference_context
    ):
        return None
    return ConversationContext(
        recent_conversation_summary=request.conversation_context.recent_conversation_summary,
        recent_messages=request.conversation_context.recent_messages,
        reference_context=request.conversation_context.reference_context,
    )


def _whole_document_target(markdown: str) -> MarkdownEditTarget:
    return MarkdownEditTarget(
        type="whole_document",
        start_line=1,
        end_line=max(1, markdown_line_count(markdown)),
    )


def _missing_current_section(
    route: AgentTurnRoute,
    target: MarkdownEditTarget,
) -> bool:
    return (
        route.edit_operation == "insert_after"
        and route.edit_destination == "target"
        and target.type != "current_section"
    )


def _with_active_markdown_reference(request: AgentTurnRequest) -> AgentTurnRequest:
    markdown_context = request.active_markdown_context
    if markdown_context is None:
        return request
    target = markdown_context.target or _whole_document_target(markdown_context.markdown)
    conversation_context = request.conversation_context or AgentConversationContext()
    reference_context = dict(conversation_context.reference_context)
    reference_context["active_markdown"] = {
        "target_type": target.type,
        "markdown": markdown_line_range(
            markdown_context.markdown,
            target.start_line,
            target.end_line,
        ),
    }
    return replace(
        request,
        conversation_context=replace(
            conversation_context,
            reference_context=reference_context,
        ),
    )


def _skill_authoring_instruction(request: AgentTurnRequest) -> str:
    context = _conversation_context_text(request)
    return f"{context}\n\n사용자의 현재 답변:\n{request.message}" if context else request.message


def _conversation_context_text(request: AgentTurnRequest) -> str | None:
    context = _to_query_conversation_context(request)
    if context is None:
        return None
    sections = [context.recent_conversation_summary or "", conversation_messages_text(context)]
    text = "\n".join(section.strip() for section in sections if section.strip())
    return text or None


def _skill_scope_type(request: AgentTurnRequest) -> SkillScopeType | None:
    return request.skill_scope_type or _scope_from_text(request.message)


def _scope_from_text(message: str) -> SkillScopeType | None:
    personal = re.search(r"(?:개인|personal)(?:\s*스킬)?", message, re.IGNORECASE) is not None
    team = re.search(r"(?:팀|현재\s*워크스페이스|team)(?:\s*스킬)?", message, re.IGNORECASE) is not None
    if personal == team:
        return None
    return "personal" if personal else "team"


def _reject_unsafe_workspace_mutation(route: AgentTurnRoute) -> AgentTurnResult:
    return AgentTurnResult(
        action="reject",
        route=replace(
            route,
            action="reject",
            confidence=1.0,
            reason="unsafe mutation request",
            edit_goal=None,
            edit_operation=None,
            edit_destination=None,
            selected_skill_id=None,
            skill_candidates=(),
            retrieval_source="none",
            document_operation="none",
            persist=False,
        ),
        message="보안상 위험한 지시나 민감정보를 Workspace에 추가하는 요청은 처리할 수 없습니다.",
    )


def _skill_authoring_message(result: SkillAuthoringResult) -> str:
    if result.status == "published" and result.proposal is not None:
        return f"게시했습니다: /{result.proposal.name}"
    return "Skill 제안을 만들었습니다. Markdown과 보안 결과를 확인한 뒤 게시해 주세요."


def _skill_instructions(skill: Skill | None) -> str | None:
    if skill is None or skill.enabled_version is None:
        return None
    return skill.enabled_version.instructions_markdown
