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
from app.modules.agent.domain.entities import (
    AgentAction,
    AgentTurnRequest,
    AgentTurnRoute,
    DocumentOperation,
    RetrievalSource,
)
from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
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
    "workspace_workflow",
    "skill_authoring",
    "skill_draft_proposal",
    "clarify",
    "reject",
}
ALLOWED_RETRIEVAL_SOURCES = {"none", "workspace", "web"}
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

        payload = {
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
        route, failures = self._complete_route(payload)
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


def build_agent_turn_router(
    *,
    provider: str | None = None,
    model: str | None = None,
) -> AgentTurnRouterPort:
    resolved_provider, resolved_model = resolve_llm_selection(provider, model)
    api_key = _api_key(resolved_provider)
    if not api_key:
        raise RuntimeError(f"Set {provider_api_key_env(resolved_provider)}.")
    prompt_path = Path(os.environ.get("AGENT_TURN_ROUTER_SYSTEM_PROMPT", str(DEFAULT_AGENT_TURN_ROUTER_PROMPT)))
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
        system_prompt=prompt_path.read_text(encoding="utf-8"),
    )


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


def _route_failures(route: AgentTurnRoute, request: AgentTurnRequest) -> list[str]:
    failures: list[str] = []
    if (
        route.action == "clarify"
        and route.edit_goal is None
        and not route.skill_candidates
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
