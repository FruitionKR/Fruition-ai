import json
import logging
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from app.core.llm_env import (
    api_key_from_env,
    float_env,
    int_env,
    optional_int_env,
    provider_api_key_env,
    resolve_llm_selection,
)
from app.modules.agent.application.ports import AgentTurnRouterPort
from app.modules.agent.domain.attached_edit_request import (
    asks_open_document_with_attachments,
    lacks_attachment_change_request,
    lacks_save_or_change_request,
    saves_result_into_document,
    selects_attachment,
    selects_every_document,
    selects_only_attachments,
)
from app.modules.agent.domain.entities import (
    AgentAction,
    AgentTurnRequest,
    CLARIFY_NOTICE_KINDS,
    REJECT_NOTICE_KINDS,
    AgentTurnRoute,
    DocumentOperation,
    NoticeKind,
    RetrievalSource,
)
from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
from app.modules.agent.domain.task_mode import (
    saves_open_document_result_as_new,
    saves_open_document_result_into_it,
    summarizes_open_document,
    task_mode_allows_action,
    task_mode_route,
)
from app.modules.markdown_edit.domain.entities import EditDestination, EditOperationType
from app.modules.skill.domain.entities import SkillCapability
from app.modules.skill.domain.policy import CAPABILITY_TOOLS
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError

DEFAULT_AGENT_TURN_ROUTER_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "agent_turn_router.system.md"
PUBLISH_SKILL_PATTERN = re.compile(
    r"(?:이대로\s*)?(?:게시|등록)(?:해|해줘|해주세요|하자)|"
    r"(?:please\s+)?(?:publish|post)(?:\s+(?:it|this|the\s+skill))?",
    re.IGNORECASE,
)
PENDING_SKILL_FOLLOWUP_PATTERN = re.compile(
    r"보안\s*(?:재)?검토|다시\s*검증|security\s*(?:re)?view|"
    r"(?:AI로\s*)?재생성|다시\s*(?:만들어|작성)|regenerate|"
    r"(?:제목|이름|커맨드|식별자).*(?:바꿔|변경|수정)|"
    r"(?:개인|팀)(?:\s*(?:스킬|skill))?(?:로|으로)?\s*(?:해|바꿔|변경|수정)",
    re.IGNORECASE,
)
ALLOWED_ACTIONS = {
    "chat_answer",
    "conversation_reply",
    "markdown_edit",
    "markdown_create",
    "folder_organize",
    "folder_lookup",
    "workspace_workflow",
    "skill_authoring",
    "skill_draft_proposal",
    "clarify",
    "reject",
}
ALLOWED_RETRIEVAL_SOURCES = {"none", "workspace", "web"}
ALLOWED_NOTICE_KINDS = CLARIFY_NOTICE_KINDS | REJECT_NOTICE_KINDS | {"skill_off"}
ALLOWED_DOCUMENT_OPERATIONS = {"none", "create", "edit"}
ALLOWED_EDIT_GOALS = {
    "shorten",
    "style_change",
    "convert_format",
    "bullet_list",
    "checklist",
    "translate",
    "cleanup",
    "template_transform",
    "create_from_chat",
    "other",
}
ALLOWED_EDIT_OPERATIONS = {"replace", "insert_after"}
ALLOWED_EDIT_DESTINATIONS = {"target", "document_end"}
JSON_OBJECT_CONTRACT_FAILURE = "model output must be a JSON object"
logger = logging.getLogger(__name__)


class ChatCompletionsTurnRouter(AgentTurnRouterPort):
    def __init__(self, client: ChatCompletionsJsonClient, system_prompt: str) -> None:
        self._client = client
        self._system_prompt = system_prompt

    def route(self, request: AgentTurnRequest) -> AgentTurnRoute:
        guarded = _local_guard(request)
        if guarded is not None:
            return guarded

        payload = route_payload(request)
        route, failures = self._complete_route(payload)
        route = _repaired_route(route, request)
        failures.extend(_route_failures(route, request))
        if failures:
            logger.warning("[Agent route 재시도] contractFailures=%s", failures)
            retry_payload = {
                **payload,
                "contract_failures": failures,
                "retry_instruction": "Correct every contract failure and return the required route JSON object again.",
            }
            route, failures = self._complete_route(
                retry_payload,
                trusted_contract_failures=failures,
            )
            route = _repaired_route(route, request)
            failures.extend(_route_failures(route, request))
            if failures:
                raise AgentTurnRouteContractError(failures)

        return route

    def _complete_route(
        self,
        payload: dict[str, object],
        trusted_contract_failures: list[str] | None = None,
    ) -> tuple[AgentTurnRoute, list[str]]:
        system_prompt = self._system_prompt
        if trusted_contract_failures:
            system_prompt += (
                "\n\nTrusted application contract failures from the previous route:\n- "
                + "\n- ".join(trusted_contract_failures)
                + "\nCorrect every listed failure in the next route."
            )
        try:
            raw = self._client.complete_json(
                system_prompt,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except JsonParseError:
            return _fallback_route(), [JSON_OBJECT_CONTRACT_FAILURE]
        return _normalize_route(raw)


def route_payload(request: AgentTurnRequest) -> dict[str, object]:
    """라우터 모델에 보낼 사용자 요청 상태다. Jev 라우터도 같은 상태를 쓴다."""
    payload = _route_state(request)
    # 기존 턴의 상태는 그대로 두고, 작업 탭 턴에만 mode를 싣는다.
    if request.mode is not None:
        payload["mode"] = request.mode
    # 첨부가 있는 턴에만 싣는다. 본문과 id는 라우터에 보내지 않는다. 제목 순서가 attached_edit_targets의 순번이다.
    count = _attachment_count(request)
    if count:
        # 내용 없는 열린 새 노트는 고칠 대상이 아니다. 편집 범위까지 빼서 열린 문서가 없는 턴과 똑같이 판단하게 한다.
        if not (request.active_markdown_context and request.active_markdown_context.markdown.strip()):
            payload["active_markdown_context"] = {"has_markdown": False, "target": None}
        payload["reference_documents"] = {
            "count": count,
            "titles": list(request.reference_document_titles or ("",) * count),
        }
        if request.active_document_title and _has_open_markdown(request):
            payload["active_markdown_context"]["title"] = request.active_document_title  # type: ignore[index]
    return payload


def _attachment_count(request: AgentTurnRequest) -> int:
    return len(set(request.reference_document_ids))


def _has_open_markdown(request: AgentTurnRequest) -> bool:
    return bool(request.active_markdown_context and request.active_markdown_context.markdown.strip())


def _route_state(request: AgentTurnRequest) -> dict[str, object]:
    return {
        "message": request.message,
        "conversation_summary": (
            request.conversation_context.recent_conversation_summary
            if request.conversation_context
            else None
        ),
        "recent_messages": (
            [
                {
                    "role": message.role,
                    "content": message.content,
                    "action": message.action,
                    "agent_route": (
                        {
                            "action": message.agent_route.action,
                            "retrieval_source": message.agent_route.retrieval_source,
                            "document_operation": message.agent_route.document_operation,
                            "persist": message.agent_route.persist,
                            "edit_goal": message.agent_route.edit_goal,
                            "edit_operation": message.agent_route.edit_operation,
                            "edit_destination": message.agent_route.edit_destination,
                            "selected_skill_id": message.agent_route.selected_skill_id,
                        }
                        if message.agent_route
                        else None
                    ),
                }
                for message in request.conversation_context.recent_messages
            ]
            if request.conversation_context
            else []
        ),
        "reference_context": (
            request.conversation_context.reference_context
            if request.conversation_context
            else {}
        ),
        "pending_skill_proposal": (
            {
                "scope_type": request.conversation_context.pending_skill_proposal.scope_type,
                "name": request.conversation_context.pending_skill_proposal.name,
                "description": request.conversation_context.pending_skill_proposal.description,
                "instructions_markdown": request.conversation_context.pending_skill_proposal.instructions_markdown,
            }
            if request.conversation_context and request.conversation_context.pending_skill_proposal
            else None
        ),
        "active_markdown_context": {
            "has_markdown": bool(request.active_markdown_context and request.active_markdown_context.markdown.strip()),
            "target": (
                {
                    "type": request.active_markdown_context.target.type,
                    "start_line": request.active_markdown_context.target.start_line,
                    "end_line": request.active_markdown_context.target.end_line,
                }
                if request.active_markdown_context and request.active_markdown_context.target
                else None
            ),
        },
        "skill_mode": request.skill_mode,
        "skill_scope_type": request.skill_scope_type,
        "skill_authoring_mode": request.skill_authoring_mode,
        "has_selected_completed_work": bool(request.skill_draft_sources),
        "allow_web_search": request.allow_web_search,
        "available_skills": [
            {
                "id": skill.id,
                "version_id": skill.version_id,
                "name": skill.name,
                "description": skill.description,
                "capabilities": list(skill.capabilities),
            }
            for skill in request.available_skills
        ],
    }


def build_agent_turn_router(
    *,
    provider: str | None = None,
    model: str | None = None,
) -> AgentTurnRouterPort:
    resolved_provider, resolved_model = resolve_llm_selection(provider, model)
    api_key = _api_key(resolved_provider)
    if not api_key:
        raise RuntimeError(f"Set {provider_api_key_env(resolved_provider)}.")
    client = ChatCompletionsJsonClient(
        ChatClientConfig(
            api_key=api_key,
            model=resolved_model,
            temperature=None,
            timeout_seconds=_int_env("AGENT_ROUTER_LLM_TIMEOUT_SECONDS", 180),
            max_tokens=_optional_int_env("AGENT_ROUTER_LLM_MAX_TOKENS"),
            json_mode=True,
            provider=resolved_provider,
        )
    )
    return ChatCompletionsTurnRouter(
        client,
        system_prompt=load_agent_turn_router_prompt(),
    )


def load_agent_turn_router_prompt() -> str:
    prompt_path = Path(os.environ.get("AGENT_TURN_ROUTER_SYSTEM_PROMPT", str(DEFAULT_AGENT_TURN_ROUTER_PROMPT)))
    return prompt_path.read_text(encoding="utf-8")


def _local_guard(request: AgentTurnRequest) -> AgentTurnRoute | None:
    lowered = request.message.lower()
    has_pending_proposal = bool(
        request.conversation_context and request.conversation_context.pending_skill_proposal
    )
    if (
        has_pending_proposal
        and (
            PUBLISH_SKILL_PATTERN.fullmatch(lowered.strip())
            or PENDING_SKILL_FOLLOWUP_PATTERN.search(lowered)
        )
    ):
        return AgentTurnRoute(
            action="skill_authoring",
            confidence=1.0,
            reason="explicit approval for pending Skill proposal",
        )
    return None


def _repaired_route(route: AgentTurnRoute, request: AgentTurnRequest) -> AgentTurnRoute:
    repaired = repair_create_route_fields(repair_insert_after_without_section(route, request), request)
    # 내용 있는 열린 문서가 없으면 그 값은 가리킬 곳이 없다. 열린 문서가 없는 턴과 똑같이 처리한다.
    if repaired.include_active_markdown and not _has_open_markdown(request):
        repaired = replace(repaired, include_active_markdown=False)
    return task_mode_route(repair_result_save_to_new_document(repaired, request), request)


def repair_result_save_to_new_document(route: AgentTurnRoute, request: AgentTurnRequest) -> AgentTurnRoute:
    """첨부 없는 턴의 "요약해서 저장해 줘"는 모델이 무엇을 골랐든 새 문서로 보낸다. 원문을 고치면 요약본으로 덮어쓴다.

    폴더 이동이 섞이면 건드리지 않는다. Skill로 새 문서를 만드는 route도 원문을 고치지 않으므로 그대로 둔다.
    """
    if not saves_open_document_result_as_new(request) or "folder-organize" in route.required_capabilities:
        return route
    if route.document_operation == "create" and route.selected_skill_id:
        return route
    # 저장하라는 말은 분명하므로 내용을 되묻지 않는다. "요약본 문서에 저장해 줘"에 모델이 되묻는 route를 내기도 한다.
    is_needless_clarify = (
        route.action == "clarify"
        and route.document_operation == "none"
        and not route.skill_candidates
        and route.notice_kind in {"chat_tab", "details", None}
    )
    if route.action not in {"markdown_edit", "workspace_workflow", "chat_answer", "conversation_reply"} and not is_needless_clarify:
        return route
    return replace(
        route,
        action="markdown_create",
        retrieval_source=route.retrieval_source if route.document_operation != "none" else "none",
        document_operation="create",
        persist=False,
        required_capabilities=("document-create",),
        edit_goal="create_from_chat",
        edit_operation=None,
        edit_destination=None,
        selected_skill_id=None,
        skill_candidates=(),
        notice_kind=None,
    )


def repair_create_route_fields(route: AgentTurnRoute, request: AgentTurnRequest) -> AgentTurnRoute:
    """첨부 턴의 새 문서 route가 편집 필드를 함께 낸 경우 새 문서 값으로 맞춘다.

    "요약해서 저장해 줘"에 모델이 edit_goal을 shorten으로 내기 쉽다. 새 문서는 원문을 건드리지 않으므로 다시 묻지 않는다.
    """
    if (
        not request.reference_document_ids
        or route.document_operation != "create"
        or route.attached_edit_targets
        or (route.edit_goal, route.edit_operation, route.edit_destination) == ("create_from_chat", None, None)
    ):
        return route
    return replace(route, edit_goal="create_from_chat", edit_operation=None, edit_destination=None)


def repair_insert_after_without_section(route: AgentTurnRoute, request: AgentTurnRequest) -> AgentTurnRoute:
    """현재 섹션이 없는데 target 뒤 삽입(insert_after+target)을 고른 route를 대상 재작성(replace)으로 바꾼다.

    "첫 문단 끝에 문장 추가"처럼 문서 안 특정 위치에 덧붙이는 요청은 섹션 경계가 없어도 수행할 수 있다.
    대상(문서 전체나 선택 범위)을 추가 내용을 포함해 다시 쓰면 위치를 지키면서 기존 내용도 보존된다
    (편집 프롬프트의 additive preservation 규칙). 섹션 앵커 없는 insert_after는 편집 단계가 실행할 수 없어
    계약 실패로 끝나므로 재시도 대신 여기서 고친다. Markdown 문서가 없으면 고치지 않고 기존 안내를 따른다.
    """
    target = request.active_markdown_context.target if request.active_markdown_context else None
    if (
        route.edit_operation != "insert_after"
        or route.edit_destination != "target"
        or (target is not None and target.type == "current_section")
        or request.active_markdown_context is None
        or not request.active_markdown_context.markdown.strip()
    ):
        return route
    if route.action in {"markdown_edit", "workspace_workflow"}:
        repaired = replace(route, edit_operation="replace")
    elif (
        route.action == "clarify"
        and route.edit_goal not in {None, "create_from_chat", "template_transform"}
        and not route.skill_candidates
    ):
        # 섹션 선택만 요구하던 clarify는 대상 재작성으로 바로 수행한다.
        repaired = replace(
            route,
            action="markdown_edit",
            edit_operation="replace",
            document_operation="edit",
            retrieval_source="none",
            persist=False,
            required_capabilities=("document-edit",),
        )
    else:
        return route
    logger.info(
        "[Agent route 보정] insert_after target without current_section -> replace action=%s",
        repaired.action,
    )
    return repaired


def _route_failures(route: AgentTurnRoute, request: AgentTurnRequest) -> list[str]:
    failures: list[str] = []
    failures.extend(_notice_failures(route, request))
    failures.extend(_attached_edit_failures(route, request))
    failures.extend(_open_document_inclusion_failures(route, request))
    failures.extend(_open_document_edit_failures(route, request))
    if route.document_operation == "create" and saves_open_document_result_into_it(request):
        failures.append(
            "the message asks to save the result into the open document, so it is an edit of that document, "
            "not document_operation create"
        )
    if route.action == "folder_lookup" and request.mode != "task":
        failures.append("folder_lookup is available only in task mode")
    if not task_mode_allows_action(request, route.action):
        failures.append(
            f"task mode without attached documents does not allow {route.action}; "
            "use clarify with document_operation none for questions or conversation"
        )
    if (
        route.action == "clarify"
        and route.edit_goal is None
        and not route.skill_candidates
        and request.mode != "task"
        and route.notice_kind != "attachment_target"
    ):
        failures.append(
            "clarify requires a supported Markdown target reason or ambiguous Skill candidates; "
            "use conversation_reply when a conversational task needs more user context"
        )
    expected_persist = route.action in {"folder_organize", "workspace_workflow"}
    if route.persist != expected_persist:
        failures.append(
            f"persist must be {str(expected_persist).lower()} for action {route.action}"
        )
    allowed_document_operations = {
        "markdown_create": {"create"},
        "markdown_edit": {"edit"},
        "workspace_workflow": {"none", "create", "edit"},
        "clarify": {"none", "edit"},
    }.get(route.action, {"none"})
    if route.document_operation not in allowed_document_operations:
        failures.append(
            f"document_operation {route.document_operation} is inconsistent with action {route.action}"
        )
    if route.document_operation == "create" and route.edit_goal != "create_from_chat":
        failures.append("document_operation create requires edit_goal create_from_chat")
    if route.document_operation == "edit" and route.edit_goal in {None, "create_from_chat"}:
        failures.append("document_operation edit requires a non-create edit_goal")
    if route.document_operation == "none" and route.edit_goal is not None:
        failures.append("document_operation none requires edit_goal null")
    if route.document_operation == "edit":
        if route.edit_operation is None:
            failures.append("document_operation edit requires edit_operation")
        if route.edit_destination is None:
            failures.append("document_operation edit requires edit_destination")
    elif route.edit_operation is not None or route.edit_destination is not None:
        failures.append("non-edit document_operation requires null edit operation and destination")
    if route.edit_operation == "replace" and route.edit_destination != "target":
        failures.append("replace edit_operation requires target edit_destination")
    if (
        route.edit_operation == "insert_after"
        and route.edit_destination == "target"
        and (
            request.active_markdown_context is None
            or request.active_markdown_context.target is None
            or request.active_markdown_context.target.type != "current_section"
        )
    ):
        failures.append("target insert_after requires a current_section target")
    if route.retrieval_source != "none" and route.action not in {
        "chat_answer",
        "markdown_create",
        "markdown_edit",
        "workspace_workflow",
    }:
        failures.append(
            f"retrieval_source {route.retrieval_source} is inconsistent with action {route.action}"
        )
    if route.action == "chat_answer" and route.retrieval_source == "none":
        failures.append("chat_answer requires workspace or web retrieval_source")
    if route.retrieval_source == "web" and request.allow_web_search is not True:
        failures.append("web retrieval requires allow_web_search true")
    required_capabilities = set(route.required_capabilities)
    handles_document = route.action in {
        "markdown_create",
        "markdown_edit",
        "workspace_workflow",
    }
    if (
        handles_document
        and route.document_operation == "create"
        and not required_capabilities.intersection({"document-create", "template"})
    ):
        failures.append("document_operation create requires document-create or template capability")
    if (
        handles_document
        and route.document_operation == "edit"
        and not required_capabilities.intersection({"document-edit", "template"})
    ):
        failures.append("document_operation edit requires document-edit or template capability")
    if route.document_operation != "create" and "document-create" in required_capabilities:
        failures.append("document-create capability requires document_operation create")
    if route.document_operation != "edit" and "document-edit" in required_capabilities:
        failures.append("document-edit capability requires document_operation edit")
    if route.document_operation == "none" and "template" in required_capabilities:
        failures.append("template capability requires a document operation")
    if "folder-organize" in required_capabilities and route.action not in {
        "folder_organize",
        "workspace_workflow",
    }:
        failures.append("folder-organize capability requires a persistent Workspace action")
    if route.action == "folder_organize" and required_capabilities != {"folder-organize"}:
        failures.append("folder_organize requires only the folder-organize capability")
    if route.action == "workspace_workflow" and not required_capabilities:
        failures.append("workspace_workflow requires at least one capability")
    if route.action not in {
        "markdown_create",
        "markdown_edit",
        "folder_organize",
        "workspace_workflow",
    } and required_capabilities:
        failures.append(f"action {route.action} must not require Skill capabilities")
    return failures


def _attached_edit_failures(route: AgentTurnRoute, request: AgentTurnRequest) -> list[str]:
    targets = route.attached_edit_targets
    if not targets:
        return []
    failures: list[str] = []
    count = _attachment_count(request)
    if any(target > count for target in targets):
        failures.append(f"attached_edit_targets must be between 1 and {count}, the number of attached documents")
    if route.action != "workspace_workflow" or route.document_operation != "edit" or not route.persist:
        failures.append("attached_edit_targets requires workspace_workflow with document_operation edit and persist true")
    if "folder-organize" in route.required_capabilities:
        failures.append("attached_edit_targets cannot be combined with folder-organize")
    if (route.edit_operation, route.edit_destination) not in {("replace", "target"), ("insert_after", "document_end")}:
        failures.append("attached_edit_targets requires replace at target or insert_after at document_end")
    if lacks_attachment_change_request(request.message, route.edit_goal):
        failures.append(
            "attached_edit_targets requires the message to explicitly ask to change the attached document; "
            "a summary or translation without that is chat_answer or conversation_reply, or markdown_create "
            "when it asks for a new document, with an empty array"
        )
    if _open_document_is_the_unnamed_target(request):
        failures.append(
            "with active Markdown the message must name an attachment to change it; a message that does not "
            "say which changes the active Markdown, so use markdown_edit with an empty array"
        )
    return failures


def _open_document_inclusion_failures(route: AgentTurnRoute, request: AgentTurnRequest) -> list[str]:
    """열린 문서까지 고치라는 요청에서 어느 한쪽이 조용히 빠지지 않게 하고, 첨부만 지목한 요청에는 열린 문서를 넣지 않는다."""
    count = _attachment_count(request)
    titles = request.reference_document_titles
    if route.include_active_markdown:
        if not route.attached_edit_targets:
            return [
                "include_active_markdown requires attached_edit_targets; changing only the active Markdown is "
                "markdown_edit with include_active_markdown false"
            ]
        names_open_title = bool(request.active_document_title and request.active_document_title in request.message)
        if (
            not names_open_title
            and not asks_open_document_with_attachments(request.message, count)
            and selects_only_attachments(request.message, count, titles)
        ):
            return ["include_active_markdown must be false because the message selects only attached documents"]
        return []
    if (
        not count
        or not _has_open_markdown(request)
        or route.document_operation != "edit"
        or route.action not in {"markdown_edit", "workspace_workflow"}
    ):
        return []
    asked = asks_open_document_with_attachments(request.message, count)
    if route.attached_edit_targets:
        if asked or selects_every_document(request.message, titles):
            return [
                "the message also includes the active Markdown, so set include_active_markdown true together with "
                "attached_edit_targets; leaving it out silently skips the open document"
            ]
    elif asked:
        return [
            "the message also includes the attached documents, so set attached_edit_targets to them with "
            "include_active_markdown true, workspace_workflow, and persist true"
        ]
    return []


def _open_document_is_the_unnamed_target(request: AgentTurnRequest) -> bool:
    """문서가 열려 있는데 첨부를 말하지 않았으면 대상은 열린 문서다. 첨부를 고르면 말하지 않은 문서가 바뀐다.

    첨부 수정은 자동 승인으로 바로 저장될 수 있어 코드로 지킨다.
    내용 없는 새 노트는 대상이 아니므로 세지 않는다.
    """
    context = request.active_markdown_context
    return (
        context is not None
        and bool(context.markdown.strip())
        and not selects_attachment(request.message)
        and not any(title and title in request.message for title in request.reference_document_titles)
        and not asks_open_document_with_attachments(request.message, _attachment_count(request))
        and not selects_every_document(request.message, request.reference_document_titles)
    )


def _open_document_edit_failures(route: AgentTurnRoute, request: AgentTurnRequest) -> list[str]:
    """요약·번역이 열린 문서를 덮어쓰지 않게 한다.

    프론트는 자동 승인이 켜져 있으면 markdown_edit도 확인 없이 적용하므로, 첨부가 있든 없든 수정을 시키지 않은 요약·번역은 막는다.
    첨부 없는 턴의 markdown_edit은 문장에 요약·번역이 있을 때만 막는다. "한 문단으로 압축해 줘"는 그 문서를 고치라는 말로 받는다.
    """
    if route.document_operation != "edit" or route.attached_edit_targets:
        return []
    if route.action not in {"markdown_edit", "workspace_workflow"}:
        return []
    if request.reference_document_ids:
        if lacks_attachment_change_request(request.message, route.edit_goal):
            return [
                "editing the open document requires the message to explicitly ask to change it; with attached "
                "documents a summary or translation without that is chat_answer or conversation_reply, or "
                "markdown_create, or workspace_workflow with document_operation create when it asks to save"
            ]
        return []
    if saves_open_document_result_as_new(request) and "folder-organize" not in route.required_capabilities:
        return [
            "saving a summary or translation of the open document leaves it untouched: use markdown_create with "
            "document_operation create unless the message says to change the document or save into it"
        ]
    if _translates_without_pointing_at_it(route, request) or (
        route.action == "markdown_edit" and summarizes_open_document(request)
    ):
        return [
            "editing the open document requires the message to explicitly ask to change it; a summary or "
            "translation without that is chat_answer with retrieval_source workspace or conversation_reply, "
            "with document_operation none, in task mode too"
        ]
    if (
        route.action == "workspace_workflow"
        and "folder-organize" not in route.required_capabilities
        and lacks_save_or_change_request(request.message, route.edit_goal)
    ):
        return [
            "a summary or translation of the open document without a save clause is chat_answer or "
            "conversation_reply; a request to change it without a save clause is markdown_edit with persist false"
        ]
    return []


def _translates_without_pointing_at_it(route: AgentTurnRoute, request: AgentTurnRequest) -> bool:
    """"영어로", "영어로 저장해 줘"는 번역이라는 말이 없어 문장 검사를 비켜 가므로 모델이 고른 편집 목표로 막는다."""
    return (
        route.edit_goal == "translate"
        and "folder-organize" not in route.required_capabilities
        and lacks_attachment_change_request(request.message, route.edit_goal)
        and not saves_result_into_document(request.message)
    )


def _notice_failures(route: AgentTurnRoute, request: AgentTurnRequest) -> list[str]:
    kind = route.notice_kind
    if kind is None:
        return []
    if kind in CLARIFY_NOTICE_KINDS:
        if route.action != "clarify" or route.document_operation != "none" or route.skill_candidates:
            return [f"notice_kind {kind} requires clarify with document_operation none and no skill candidates"]
        if kind == "attachment_target" and not request.reference_document_ids:
            return ["notice_kind attachment_target requires attached documents"]
        if kind == "web_disabled" and request.allow_web_search is True:
            return ["notice_kind web_disabled requires allow_web_search false"]
        return []
    if kind in REJECT_NOTICE_KINDS:
        return [] if route.action == "reject" else [f"notice_kind {kind} requires reject"]
    # skill_off: Skill 사용이 꺼져 있는데 Skill로 하라는 요청을 Skill 없이 처리할 때다.
    if route.action not in {"markdown_edit", "markdown_create", "workspace_workflow"} or request.skill_mode != "off":
        return ["notice_kind skill_off requires a document action with skill_mode off"]
    return []


def _normalize_route(value: dict[str, Any]) -> tuple[AgentTurnRoute, list[str]]:
    failures: list[str] = []
    raw_action = value.get("action")
    if not isinstance(raw_action, str) or raw_action not in ALLOWED_ACTIONS:
        failures.append("action must be a supported value")
        action: AgentAction = "chat_answer"
    else:
        action = raw_action

    raw_confidence = value.get("confidence")
    if (
        not isinstance(raw_confidence, (int, float))
        or isinstance(raw_confidence, bool)
        or not 0.0 <= float(raw_confidence) <= 1.0
    ):
        failures.append("confidence must be a number between 0 and 1")
        confidence = 0.0
    else:
        confidence = float(raw_confidence)

    raw_reason = value.get("reason")
    if not isinstance(raw_reason, str) or not raw_reason.strip():
        failures.append("reason must be a non-empty string")
        reason = ""
    else:
        reason = raw_reason.strip()

    if "edit_goal" not in value:
        failures.append("edit_goal is required")
    raw_edit_goal = value.get("edit_goal")
    if raw_edit_goal is not None and (
        not isinstance(raw_edit_goal, str) or raw_edit_goal not in ALLOWED_EDIT_GOALS
    ):
        failures.append("edit_goal must be a supported value or null")
        edit_goal = None
    else:
        edit_goal = _optional_text(raw_edit_goal)

    if "edit_operation" not in value:
        failures.append("edit_operation is required")
    raw_edit_operation = value.get("edit_operation")
    if raw_edit_operation is not None and (
        not isinstance(raw_edit_operation, str)
        or raw_edit_operation not in ALLOWED_EDIT_OPERATIONS
    ):
        failures.append("edit_operation must be replace, insert_after, or null")
        edit_operation = None
    else:
        edit_operation = cast(EditOperationType | None, raw_edit_operation)

    if "edit_destination" not in value:
        failures.append("edit_destination is required")
    raw_edit_destination = value.get("edit_destination")
    if raw_edit_destination is not None and (
        not isinstance(raw_edit_destination, str)
        or raw_edit_destination not in ALLOWED_EDIT_DESTINATIONS
    ):
        failures.append("edit_destination must be target, document_end, or null")
        edit_destination = None
    else:
        edit_destination = cast(EditDestination | None, raw_edit_destination)

    raw_retrieval_source = value.get("retrieval_source")
    if not isinstance(raw_retrieval_source, str) or raw_retrieval_source not in ALLOWED_RETRIEVAL_SOURCES:
        failures.append("retrieval_source must be none, workspace, or web")
        retrieval_source: RetrievalSource = "none"
    else:
        retrieval_source = raw_retrieval_source

    raw_document_operation = value.get("document_operation")
    if not isinstance(raw_document_operation, str) or raw_document_operation not in ALLOWED_DOCUMENT_OPERATIONS:
        failures.append("document_operation must be none, create, or edit")
        document_operation: DocumentOperation = "none"
    else:
        document_operation = raw_document_operation

    raw_persist = value.get("persist")
    if not isinstance(raw_persist, bool):
        failures.append("persist must be a boolean")
        persist = False
    else:
        persist = raw_persist

    raw_selected_skill_id = value.get("selected_skill_id")
    if raw_selected_skill_id is not None and not isinstance(raw_selected_skill_id, str):
        failures.append("selected_skill_id must be a string or null")
        selected_skill_id = None
    else:
        selected_skill_id = _optional_text(raw_selected_skill_id)

    raw_skill_candidates = value.get("skill_candidates", [])
    if not isinstance(raw_skill_candidates, list) or not all(
        isinstance(candidate, str) and candidate.strip() for candidate in raw_skill_candidates
    ):
        failures.append("skill_candidates must be an array of non-empty strings")
        skill_candidates: tuple[str, ...] = ()
    else:
        skill_candidates = tuple(candidate.strip() for candidate in raw_skill_candidates)

    # 선택 항목이다. 없으면 기존 안내 문구를 쓴다.
    raw_notice_kind = value.get("notice_kind")
    if raw_notice_kind is not None and raw_notice_kind not in ALLOWED_NOTICE_KINDS:
        failures.append("notice_kind must be a supported value or null")
        notice_kind: NoticeKind | None = None
    else:
        notice_kind = cast(NoticeKind | None, raw_notice_kind)

    raw_attached_targets = value.get("attached_edit_targets", [])
    if (
        not isinstance(raw_attached_targets, list)
        or not all(type(target) is int and target >= 1 for target in raw_attached_targets)
        or len(set(raw_attached_targets)) != len(raw_attached_targets)
    ):
        failures.append("attached_edit_targets must be an array of unique positive integers")
        attached_edit_targets: tuple[int, ...] = ()
    else:
        attached_edit_targets = tuple(sorted(raw_attached_targets))

    include_active_markdown = value.get("include_active_markdown", False)
    if not isinstance(include_active_markdown, bool):
        failures.append("include_active_markdown must be a boolean")
        include_active_markdown = False

    raw_required_capabilities = value.get("required_capabilities")
    if (
        not isinstance(raw_required_capabilities, list)
        or not all(
            isinstance(capability, str) and capability in CAPABILITY_TOOLS
            for capability in raw_required_capabilities
        )
        or len(set(raw_required_capabilities)) != len(raw_required_capabilities)
    ):
        failures.append("required_capabilities must be an array of unique supported capabilities")
        required_capabilities: tuple[SkillCapability, ...] = ()
    else:
        required_capabilities = tuple(
            sorted(cast(SkillCapability, capability) for capability in raw_required_capabilities)
        )

    return AgentTurnRoute(
        action=action,
        confidence=confidence,
        reason=reason,
        edit_goal=edit_goal,
        edit_operation=edit_operation,
        edit_destination=edit_destination,
        selected_skill_id=selected_skill_id,
        skill_candidates=skill_candidates,
        retrieval_source=retrieval_source,
        document_operation=document_operation,
        persist=persist,
        required_capabilities=required_capabilities,
        notice_kind=notice_kind,
        attached_edit_targets=attached_edit_targets,
        include_active_markdown=include_active_markdown,
    ), failures


def _fallback_route() -> AgentTurnRoute:
    return AgentTurnRoute(
        action="conversation_reply",
        confidence=0.0,
        reason="",
    )


def _api_key(provider: str | None = None) -> str | None:
    return api_key_from_env(
        provider=provider,
    )


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _float_env(name: str, default: float) -> float:
    return float_env(name, default)


def _int_env(name: str, default: int) -> int:
    return int_env(name, default)


def _optional_int_env(name: str) -> int | None:
    return optional_int_env(name)
