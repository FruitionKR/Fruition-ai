import hashlib
import re
from collections.abc import Callable
from dataclasses import replace

from app.modules.agent.application.ports import (
    AgentTurnRouterPort,
    ConversationReplierPort,
    FolderReferenceResolverPort,
    FolderSourceReaderPort,
    FolderTreeReaderPort,
)
from app.modules.agent.domain.entities import (
    ActiveMarkdownContext,
    AgentAction,
    AgentConversationContext,
    AgentTurnRequest,
    AgentTurnResult,
    AgentTurnRoute,
    PendingSkillProposal,
    RetrievalSource,
)
from app.modules.agent.domain.exceptions import AgentConfigurationError
from app.modules.agent.domain.folder_lookup import (
    FolderLookupUnavailableError,
    folder_lookup_message,
    pick_folders,
)
from app.modules.agent.domain.folder_sources import (
    MAX_FOLDER_SOURCE_DOCUMENTS,
    FolderSourceUnavailableError,
    SelectedFolderSources,
    folder_source_notice,
    select_folder_sources,
)
from app.modules.agent.domain.task_mode import is_task_mode, task_mode_route
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
from app.modules.query.application.ports import ConversationSummarizerPort, QueryEventPublisherPort
from app.modules.query.application.query_event import publish_query_event
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
from app.modules.skill.domain.instruction_placeholders import (
    EDIT_PAYLOAD_DOCUMENT_REFERENCE,
    REFERENCE_CONTEXT_DOCUMENT_REFERENCE,
    REFERENCE_CONTEXT_FOLDER_REFERENCE,
    render_skill_instructions,
)
from app.modules.skill.domain.policy import validate_skill_name
from app.modules.skill.domain.safety import inspect_skill_instructions

CLARIFY_MARKDOWN_TARGET_MESSAGE = "수정할 Markdown 범위를 선택한 뒤 다시 요청해 주세요."
CLARIFY_MARKDOWN_DOCUMENT_MESSAGE = "수정할 Markdown 문서를 연 뒤 다시 요청해 주세요."
CLARIFY_TASK_MODE_QUESTION_MESSAGE = (
    "작업 탭에서는 문서 편집, 폴더 정리, 스킬 실행을 요청할 수 있어요. "
    "위키 내용에 대한 질문은 채팅 탭에서 해 주세요."
)
DEFERRED_TEMPLATE_MESSAGE = "template 기반 전체 문서 재구성은 이후 단계에서 다루겠습니다. 현재는 선택 영역, 현재 섹션, 또는 문서 전체의 일반 편집만 지원합니다."
CLARIFY_INSERT_AFTER_TARGET_MESSAGE = "내용을 추가할 현재 섹션을 선택한 뒤 다시 요청해 주세요."
CLARIFY_SKILL_MESSAGE = "이 요청에 적용할 Skill을 선택하거나 Skill 없이 계속해 주세요."
CLARIFY_INCOMPATIBLE_SKILL_MESSAGE = (
    "선택한 Skill이 요청한 모든 작업을 지원하지 않습니다. "
    "다른 Skill을 선택하거나 요청 범위를 줄여 주세요."
)
CLARIFY_SKILL_SELECTION_MESSAGE = "이 Skill은 선택한 부분에만 적용돼요. 수정할 부분을 선택한 뒤 다시 요청해 주세요."
CLARIFY_SKILL_RESULT_MODE_MESSAGE = (
    "이 Skill은 새 문서로 만들 수도, 지금 문서를 수정할 수도 있어요. "
    "새 문서로 만들까요, 지금 문서를 수정할까요?"
)
# result_mode=auto Skill에서 라우터 확신도가 이보다 낮으면 새 문서/수정을 되묻는다.
AUTO_RESULT_MODE_MIN_CONFIDENCE = 0.7
# 문서 생성·편집을 할 수 있는 Skill 기능. template은 둘 다 포함한다.
CREATE_CAPABILITIES = frozenset({"template", "document-create"})
EDIT_CAPABILITIES = frozenset({"template", "document-edit"})
EMPTY_FOLDER_SOURCE_MESSAGE = (
    "이 폴더 바로 아래에 읽을 수 있는 Markdown 문서가 없어요. 문서가 있는 폴더나 문서를 골라 다시 실행해 주세요."
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
        event_publisher: QueryEventPublisherPort | None = None,
        folder_source_reader: FolderSourceReaderPort | None = None,
        folder_tree_reader: FolderTreeReaderPort | None = None,
        folder_reference_resolver: FolderReferenceResolverPort | None = None,
        on_chat_answer: Callable[[], None] | None = None,
    ) -> None:
        self._on_chat_answer = on_chat_answer
        self._event_publisher = event_publisher
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
        self._folder_source_reader = folder_source_reader
        self._folder_tree_reader = folder_tree_reader
        self._folder_reference_resolver = folder_reference_resolver

    def execute(self, request: AgentTurnRequest) -> AgentTurnResult:
        if not request.message.strip():
            raise ValueError("message is required.")

        publish_query_event(self._event_publisher, "agent.routing", "요청과 사용할 수 있는 스킬을 확인하고 있어요.")
        result = self._execute(request)
        publish_query_event(self._event_publisher, "agent.finalizing", "처리 결과와 대화 내용을 정리하고 있어요.")
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
        if selection.explicit_skill_id is not None and not request.message.strip():
            return self._execute_bare_skill_command(request, selection.skills[0])
        routed = self._router.route(request)
        resolved = selection.resolve_route(
            _create_skill_route(routed, _routed_skill(selection, routed))
        )
        # 작업 탭은 답변 갈래를 쓰지 않는다. 어떤 라우터가 골랐든 채팅 탭 안내로 바꾼다.
        route = task_mode_route(resolved.route, request)
        selected_skill = resolved.skill
        action_message = {
            "markdown_edit": "문서 편집 요청으로 확인했어요.",
            "markdown_create": "새 문서 작성 요청으로 확인했어요.",
            "chat_answer": "질문에 답할 근거를 찾고 있어요.",
            "conversation_reply": "대화 맥락에 맞는 답변을 작성하고 있어요.",
            "folder_organize": "폴더 정리 계획을 준비하고 있어요.",
            "folder_lookup": "폴더 내용을 확인하고 있어요.",
            "workspace_workflow": "문서 작업 계획을 준비하고 있어요.",
            "skill_authoring": "스킬 작성 요청을 처리하고 있어요.",
            "skill_draft_proposal": "완료한 작업을 바탕으로 스킬 제안을 준비하고 있어요.",
            "clarify": "추가로 확인할 내용을 정리하고 있어요.",
            "reject": "요청의 지원 범위를 확인하고 있어요.",
        }[route.action]
        publish_query_event(self._event_publisher, "agent.routed", action_message)
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
        if route.action in {"markdown_create", "markdown_edit"} and selected_skill is not None:
            route = _route_for_result_mode(route, selected_skill)
            # 열린 문서가 없으면 수정할 대상이 없어 새 문서 판단은 애매하지 않다.
            if route.action == "markdown_create" and _has_active_markdown(request) and _is_ambiguous_auto(route, selected_skill):
                return _clarify_result_mode(route)
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
                    try:
                        create_request, _ = self._markdown_create_request(request, route, selected_skill)
                    except FolderSourceUnavailableError as exc:
                        return _clarify_document_change(route, str(exc))
                    creation_markdown = self._markdown_create_use_case.execute(create_request).document.markdown
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
                target = _skill_edit_target(selected_skill, markdown_context)
                if target is None:
                    return _clarify_document_change(route, CLARIFY_SKILL_SELECTION_MESSAGE)
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
                            skill_instructions=_skill_instructions(selected_skill, request, document_in_payload=True),
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
                    target_folder_id=request.folder_id,
                    rule_summary=(
                        selected_skill.enabled_version.description
                        if selected_skill is not None and selected_skill.enabled_version is not None
                        else None
                    ),
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

        if route.action == "folder_lookup":
            return self._execute_folder_lookup(request, route)

        if route.action == "clarify":
            if is_task_mode(request) and route.edit_goal is None:
                message = CLARIFY_TASK_MODE_QUESTION_MESSAGE
            elif is_task_mode(request) and not _has_active_markdown(request):
                message = CLARIFY_MARKDOWN_DOCUMENT_MESSAGE
            elif route.edit_goal == "template_transform":
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

    def _execute_folder_lookup(self, request: AgentTurnRequest, route: AgentTurnRoute) -> AgentTurnResult:
        if (
            self._folder_tree_reader is None
            or self._folder_reference_resolver is None
            or not request.workspace_id
            or not request.user_id
        ):
            raise AgentConfigurationError("Folder lookup requires workspace_id, user_id, a tree reader, and a resolver.")
        try:
            folders = self._folder_tree_reader.read(workspace_id=request.workspace_id, user_id=request.user_id)
            referenced = self._folder_reference_resolver.resolve(request, folders) if folders else []
        except FolderLookupUnavailableError as exc:
            return AgentTurnResult(action="folder_lookup", route=route, message=str(exc))
        matches = pick_folders(folders, referenced, request.folder_id)
        return AgentTurnResult(action="folder_lookup", route=route, message=folder_lookup_message(matches))

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
        # Record the actual branch before retrieval/generation can fail or be interrupted.
        if self._on_chat_answer is not None:
            self._on_chat_answer()
        return AgentTurnResult(
            action="chat_answer",
            route=route,
            query_answer=self._answer_query(
                request,
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
        if _is_ambiguous_auto(route, selected_skill):
            return _clarify_result_mode(route)
        target = _skill_edit_target(selected_skill, markdown_context)
        if target is None:
            return _clarify_document_change(route, CLARIFY_SKILL_SELECTION_MESSAGE)
        if _missing_current_section(route, target):
            return _clarify_document_change(
                route,
                CLARIFY_INSERT_AFTER_TARGET_MESSAGE,
            )
        reference_context = self._resolve_reference_context(request, route)
        publish_query_event(self._event_publisher, "agent.generating", f"{_markdown_basis(selected_skill)} 편집안을 작성하고 있어요.")
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
                skill_instructions=_skill_instructions(selected_skill, request, document_in_payload=True),
                output_language=request.output_language,
            )
        )
        resolved_route = _resolved_edit_route(route, result.edit)
        return AgentTurnResult(
            action="markdown_edit",
            route=resolved_route,
            edit=result.edit,
            message=f"{_markdown_basis(selected_skill)} 편집안을 작성했어요.\n\n{result.edit.summary}\n\n아직 문서에 적용하지 않았어요. 편집안을 확인하고 적용해 주세요.",
            source_markdown_sha256=_markdown_sha256(markdown_context.markdown),
        )

    def _execute_bare_skill_command(self, request: AgentTurnRequest, skill: Skill) -> AgentTurnResult:
        """`/slug`만 보낸 요청. 할 일이 비어 있어 라우터에 맡기지 않고 Skill 정의와 고른 대상으로 실행한다.

        - 폴더 정리 Skill: 고른 폴더(`folder_id`, 없으면 워크스페이스 전체)의 정리 계획 run을 만든다.
        - 결과 방식 edit: 열린(고른) 문서를 Skill 지침대로 고친 편집안을 만든다. 문서가 없으면 문서를 고르라고 되묻는다.
        - 결과 방식 create: Skill 지침으로 새 문서 초안을 만든다. 원문이 필요한 Skill(requires_source)이면 고른 문서,
          문서가 없으면 고른 폴더 바로 아래 Markdown 문서들을 원문으로 쓴다. 원문이 필요 없으면 위치로만 쓴다.
        - 할 일이 없으므로 앞선 대화 내용은 원문으로 쓰지 않는다(명시한 문서·폴더만 원문이다).
        - 결과 방식 auto: 고른 문서가 있으면 새 문서/수정 중 무엇인지 되묻고, 없으면 새 문서 초안을 만든다.
        """
        version = skill.enabled_version
        assert version is not None  # 명시 선택 단계에서 활성 버전을 확인했다.
        mode = _bare_command_mode(version.capabilities, version.result_mode)
        if mode is None:
            return self._start_bare_folder_plan(request, skill)
        has_document = _has_active_markdown(request)
        if mode == "edit" or (mode == "auto" and has_document):
            route = AgentTurnRoute(
                action="markdown_edit",
                confidence=1.0,
                reason="Bare Skill command applies the Skill to the selected document.",
                edit_goal="other",
                edit_operation="replace",
                edit_destination="target",
                selected_skill_id=skill.id,
                document_operation="edit",
                required_capabilities=("document-edit",),
            )
            if mode == "auto":
                return _clarify_result_mode(route)
            publish_query_event(self._event_publisher, "agent.routed", "문서 편집 요청으로 확인했어요.")
            return self._execute_markdown_edit(
                replace(request, message=f"'{version.name}' Skill의 지침을 이 문서에 적용해 주세요."),
                route,
                skill,
            )
        route = AgentTurnRoute(
            action="markdown_create",
            confidence=1.0,
            reason="Bare Skill command creates a document from the Skill.",
            edit_goal="create_from_chat",
            selected_skill_id=skill.id,
            document_operation="create",
            required_capabilities=("document-create",),
        )
        publish_query_event(self._event_publisher, "agent.routed", "새 문서 작성 요청으로 확인했어요.")
        source = _create_source(request, skill)
        basis = {
            "document": "고른 문서를 원문으로 삼아 ",
            "folder": "고른 폴더의 문서들을 원문으로 삼아 ",
            "none": "",
        }[source]
        message = f"'{version.name}' Skill의 지침과 형식으로 {basis}새 문서 초안을 작성해 주세요."
        return self._execute_markdown_create(replace(request, message=message), route, skill, bare=True)

    def _start_bare_folder_plan(self, request: AgentTurnRequest, skill: Skill) -> AgentTurnResult:
        """폴더 정리 Skill의 `/slug`. 고른 폴더(없으면 워크스페이스 전체)로 승인형 정리 계획 run을 만든다."""
        if self._agent_run_starter is None or not request.workspace_id or not request.user_id:
            raise AgentConfigurationError("Workspace workflow requires workspace_id and user_id.")
        version = skill.enabled_version
        assert version is not None
        scope = "이 폴더" if request.folder_id else "워크스페이스"
        instruction = f"'{version.name}' Skill의 지침에 따라 {scope}를 정리해 주세요."
        route = AgentTurnRoute(
            action="folder_organize",
            confidence=1.0,
            reason="Bare Skill command organizes the selected folder.",
            selected_skill_id=skill.id,
            persist=True,
            required_capabilities=("folder-organize",),
        )
        publish_query_event(self._event_publisher, "agent.routed", "폴더 정리 계획을 준비하고 있어요.")
        run_id, run_status = self._agent_run_starter.start(
            StartAgentRunRequest(
                workspace_id=request.workspace_id,
                user_id=request.user_id,
                instruction=instruction,
                provider=request.provider,
                model=request.model,
                action=route.action,
                skill_version_id=version.id,
                target_folder_id=request.folder_id,
                rule_summary=version.description,
            )
        )
        return AgentTurnResult(action=route.action, route=route, run_id=run_id, run_status=run_status)

    def _execute_markdown_create(
        self,
        request: AgentTurnRequest,
        route: AgentTurnRoute,
        selected_skill: Skill | None,
        *,
        bare: bool = False,
    ) -> AgentTurnResult:
        try:
            create_request, source_notice = self._markdown_create_request(request, route, selected_skill, bare=bare)
        except FolderSourceUnavailableError as exc:
            return _clarify_document_change(route, str(exc))
        publish_query_event(self._event_publisher, "agent.generating", f"{_markdown_basis(selected_skill)} 문서 초안을 작성하고 있어요.")
        result = self._markdown_create_use_case.execute(create_request)
        notice = f"\n\n{source_notice}" if source_notice else ""
        return AgentTurnResult(
            action="markdown_create",
            route=route,
            generated_markdown=result.document,
            message=f"{_markdown_basis(selected_skill)} 문서 초안을 작성했어요.\n\n{result.document.summary}{notice}\n\n아직 문서를 만들지 않았어요. 초안을 확인하고 생성해 주세요.",
        )

    def _markdown_create_request(
        self,
        request: AgentTurnRequest,
        route: AgentTurnRoute,
        selected_skill: Skill | None,
        *,
        bare: bool = False,
    ) -> tuple[MarkdownCreateRequest, str | None]:
        """새 문서 요청과 결과 메시지에 덧붙일 원문 안내.

        명시한 Skill이 원문을 필요로 하면(requires_source) 고른 문서를 source_document로, 문서 없이 폴더를 골랐으면
        폴더 바로 아래 Markdown 문서들을 source_documents로 싣는다. 원문이 필요 없는 Skill은 고른 문서가 있어도
        원문으로 쓰지 않는다(새 문서 위치로만 쓴다).
        `/slug`만 보낸 실행(bare)은 앞선 대화를 원문처럼 섞지 않도록 대화 맥락을 싣지 않는다.

        폴더를 읽을 수 없거나 읽을 문서가 없으면 FolderSourceUnavailableError를 낸다.
        """
        reference_context = {} if bare else self._resolve_reference_context(request, route)
        source = _create_source(request, selected_skill)
        notice = None
        if source == "document":
            assert request.active_markdown_context is not None
            reference_context["source_document"] = {
                "document_id": request.document_id,
                "markdown": request.active_markdown_context.markdown,
            }
        elif source == "folder":
            sources = self._read_folder_sources(request)
            reference_context["source_documents"] = [
                {
                    "document_id": document.document_id,
                    "title": document.title,
                    "updated_at": document.updated_at,
                    "markdown": document.markdown,
                }
                for document in sources.documents
            ]
            notice = folder_source_notice(sources)
        return (
            MarkdownCreateRequest(
                instruction=request.message,
                workspace_id=request.workspace_id,
                user_id=request.user_id,
                conversation_summary=None if bare else _conversation_context_text(request),
                reference_context=reference_context,
                skill_instructions=_skill_instructions(
                    selected_skill,
                    request,
                    document_in_payload=False,
                    document_in_reference_context=source == "document",
                    folder_in_reference_context=source == "folder",
                    inline_active_document=not _explicit_without_source(request, selected_skill),
                ),
                output_language=request.output_language,
            ),
            notice,
        )

    def _read_folder_sources(self, request: AgentTurnRequest) -> SelectedFolderSources:
        if self._folder_source_reader is None or not request.workspace_id or not request.user_id:
            raise AgentConfigurationError("Folder source reading requires workspace_id, user_id, and a reader.")
        assert request.folder_id is not None
        publish_query_event(self._event_publisher, "agent.reading_sources", "고른 폴더의 문서를 읽고 있어요.")
        listing = self._folder_source_reader.read(
            workspace_id=request.workspace_id,
            user_id=request.user_id,
            folder_id=request.folder_id,
            max_documents=MAX_FOLDER_SOURCE_DOCUMENTS,
        )
        sources = select_folder_sources(listing)
        if not sources.documents:
            raise FolderSourceUnavailableError(EMPTY_FOLDER_SOURCE_MESSAGE)
        return sources

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
                requires_source=proposal.requires_source,
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
                    requires_source=proposal.requires_source,
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
                    requires_source=proposal.requires_source,
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


def _markdown_basis(skill: Skill | None) -> str:
    if skill is not None and skill.enabled_version is not None:
        return f"‘{skill.enabled_version.name}’ 스킬을 사용해"
    return "스킬 없이 요청 내용을 바탕으로"


def _skill_instructions(
    skill: Skill | None,
    request: AgentTurnRequest,
    *,
    document_in_payload: bool,
    document_in_reference_context: bool = False,
    folder_in_reference_context: bool = False,
    inline_active_document: bool = True,
) -> str | None:
    """Skill 지침의 자리표시자를 이번 요청 값으로 채운다.

    편집처럼 문서 본문이 이미 모델 payload(markdown 또는 reference_context.source_document)에 있으면
    `{문서}`는 본문을 다시 싣지 않고 그 위치를 가리킨다.
    """
    if skill is None or skill.enabled_version is None:
        return None
    context = request.active_markdown_context
    if document_in_payload:
        document_reference: str | None = EDIT_PAYLOAD_DOCUMENT_REFERENCE
    elif document_in_reference_context:
        document_reference = REFERENCE_CONTEXT_DOCUMENT_REFERENCE
    elif folder_in_reference_context:
        document_reference = REFERENCE_CONTEXT_FOLDER_REFERENCE
    else:
        document_reference = None
    inline_document = (
        context.markdown
        if context is not None and document_reference is None and inline_active_document
        else None
    )
    return render_skill_instructions(
        skill.enabled_version.instructions_markdown,
        request=request.message,
        selection=_selection_text(context),
        document=inline_document,
        document_reference=document_reference,
    )


def _create_source(request: AgentTurnRequest, skill: Skill | None) -> str:
    """명시한(`/slug`·skill_id) Skill로 새 문서를 만들 때 원문으로 쓸 대상: document·folder·none.

    원문이 필요한 Skill(requires_source)만 원문을 쓴다. 고른 문서가 있으면 그 문서, 없고 폴더를 골랐으면 그 폴더다.
    원문이 필요 없는 Skill은 문서·폴더를 골라도 새 문서 위치로만 쓴다(프론트가 문서의 부모 폴더를 folder_id로 보낸다).
    """
    version = skill.enabled_version if skill is not None else None
    if version is None or request.skill_mode != "explicit" or not version.requires_source:
        return "none"
    if _has_active_markdown(request):
        return "document"
    if request.folder_id:
        return "folder"
    return "none"


def _explicit_without_source(request: AgentTurnRequest, skill: Skill | None) -> bool:
    """명시한 Skill이 원문이 필요 없는 Skill이면 열린 문서 본문을 `{문서}`에도 넣지 않는다."""
    version = skill.enabled_version if skill is not None else None
    return version is not None and request.skill_mode == "explicit" and not version.requires_source


def _bare_command_mode(
    capabilities: tuple[str, ...],
    result_mode: str | None,
) -> str | None:
    """`/slug`만 보낸 Skill의 결과 방식. 문서 기능이 없는 폴더 정리 Skill은 None이다.

    결과 방식이 없는(이전에 게시한) Skill은 기능으로 정한다: 생성·편집 둘 다면 auto.
    """
    can_create = bool(CREATE_CAPABILITIES & set(capabilities))
    can_edit = bool(EDIT_CAPABILITIES & set(capabilities))
    if not can_create and not can_edit:
        return None
    if result_mode is not None:
        return result_mode
    if can_create and can_edit:
        return "auto"
    return "create" if can_create else "edit"


def _selection_text(context: ActiveMarkdownContext | None) -> str | None:
    if context is None or context.target is None or context.target.type != "selection":
        return None
    target = context.target
    if (
        target.start_line < 1
        or target.end_line < target.start_line
        or target.end_line > markdown_line_count(context.markdown)
    ):
        return None
    return markdown_line_range(context.markdown, target.start_line, target.end_line)


def _skill_edit_target(skill: Skill | None, markdown_context: ActiveMarkdownContext) -> MarkdownEditTarget | None:
    """Skill의 실행 대상(targets)으로 편집 범위를 정한다. 선택 범위가 필요한데 없으면 None이다."""
    target = markdown_context.target
    whole = _whole_document_target(markdown_context.markdown)
    targets = skill.enabled_version.targets if skill is not None and skill.enabled_version is not None else ()
    if targets == ("selection",):
        return target if target is not None and target.type == "selection" else None
    if targets == ("current_document",):
        return whole
    return target or whole


def _route_for_result_mode(route: AgentTurnRoute, skill: Skill) -> AgentTurnRoute:
    """결과 방식이 정해진 Skill(create·edit)은 라우터가 고른 문서 동작을 그 방식으로 맞춘다."""
    version = skill.enabled_version
    result_mode = version.result_mode if version is not None else None
    if result_mode == "create" and route.action == "markdown_edit":
        return replace(
            route,
            action="markdown_create",
            edit_goal="create_from_chat",
            edit_operation=None,
            edit_destination=None,
            document_operation="create",
            required_capabilities=("document-create",),
        )
    if result_mode == "edit" and route.action == "markdown_create":
        return replace(
            route,
            action="markdown_edit",
            edit_goal="other",
            edit_operation="replace",
            edit_destination="target",
            document_operation="edit",
            required_capabilities=("document-edit",),
        )
    return route


def _routed_skill(selection: PreparedSkillSelection, route: AgentTurnRoute) -> Skill | None:
    skill_id = selection.explicit_skill_id or route.selected_skill_id
    return next((skill for skill in selection.skills if skill.id == skill_id), None)


def _create_skill_route(route: AgentTurnRoute, skill: Skill | None) -> AgentTurnRoute:
    """결과 방식이 create인 Skill은 기준 문서가 없어도(루트·폴더만 고른 턴 포함) 새 문서로 실행한다.

    열린 문서가 없으면 라우터는 편집 요청을 문서 편집이나 대상 확인(clarify)으로 둔다. create Skill은 기준 문서를
    고치지 않으므로, 편집 기능이 없는 Skill이 지원 여부 검사에서 빠지기 전에 새 문서 생성으로 맞춘다.
    """
    version = skill.enabled_version if skill is not None else None
    if skill is not None and version is not None and version.result_mode == "edit" and route.action == "markdown_create":
        # 반대로 edit Skill은 라우터가 새 문서를 골라도 지원 검사에서 빠지기 전에 문서 편집으로 맞춘다.
        return _route_for_result_mode(route, skill)
    if version is None or version.result_mode != "create":
        return route
    create_fields = {
        "edit_goal": "create_from_chat",
        "edit_operation": None,
        "edit_destination": None,
        "document_operation": "create",
        "required_capabilities": ("document-create",),
        "selected_skill_id": skill.id,
    }
    if route.action == "workspace_workflow" and route.document_operation == "edit":
        return replace(route, **create_fields)
    if route.action == "markdown_edit" or (
        route.action == "clarify" and route.document_operation == "edit" and not route.skill_candidates
    ):
        return replace(route, action="markdown_create", persist=False, **create_fields)
    return route


def _is_ambiguous_auto(route: AgentTurnRoute, skill: Skill | None) -> bool:
    version = skill.enabled_version if skill is not None else None
    return (
        version is not None
        and version.result_mode == "auto"
        and route.confidence < AUTO_RESULT_MODE_MIN_CONFIDENCE
    )


def _has_active_markdown(request: AgentTurnRequest) -> bool:
    context = request.active_markdown_context
    return context is not None and bool(context.markdown.strip())


def _clarify_result_mode(route: AgentTurnRoute) -> AgentTurnResult:
    return AgentTurnResult(
        action="clarify",
        route=_non_mutating_route(route, action="clarify", reason="Skill result mode is ambiguous."),
        message=CLARIFY_SKILL_RESULT_MODE_MESSAGE,
    )
