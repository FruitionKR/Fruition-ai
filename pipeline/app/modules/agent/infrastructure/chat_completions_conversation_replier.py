import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.llm_env import (
    api_key_from_env,
    int_env,
    optional_int_env,
    provider_api_key_env,
    resolve_llm_selection,
)
from app.core.response_preferences import with_response_preferences
from app.modules.agent.application.ports import ConversationReplierPort
from app.modules.agent.domain.entities import AgentTurnRequest
from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError

DEFAULT_CONVERSATION_REPLY_PROMPT = (
    Path(__file__).resolve().parents[4] / "prompts" / "conversation_reply.system.md"
)
JSON_OBJECT_CONTRACT_FAILURE = "conversation reply output must be a JSON object"
PRODUCT_TIMEZONE = ZoneInfo("Asia/Seoul")


class ChatCompletionsConversationReplier(ConversationReplierPort):
    def __init__(self, client: ChatCompletionsJsonClient, system_prompt: str) -> None:
        self._client = client
        self._system_prompt = system_prompt

    def reply(self, request: AgentTurnRequest) -> str:
        current_date = _current_date()
        conversation = request.conversation_context
        payload = {
            "message": request.message,
            "conversation_summary": (
                conversation.recent_conversation_summary if conversation else None
            ),
            "recent_messages": (
                [
                    {
                        "role": message.role,
                        "content": message.content,
                        "action": message.action,
                    }
                    for message in conversation.recent_messages
                ]
                if conversation
                else []
            ),
            "reference_context": conversation.reference_context if conversation else {},
        }
        system_prompt = with_response_preferences(
            f"{self._system_prompt}\n\n# Trusted Runtime Context\nCurrent date: {current_date}",
            request.output_language,
            request.response_length,
        )
        try:
            raw = self._client.complete_json(
                system_prompt,
                json.dumps(payload, ensure_ascii=False, indent=2),
                trusted_identifiers=(current_date,),
            )
        except JsonParseError:
            retry_payload = {
                **payload,
                "contract_failures": [JSON_OBJECT_CONTRACT_FAILURE],
                "retry_instruction": "Return the required JSON object again.",
            }
            try:
                raw = self._client.complete_json(
                    system_prompt,
                    json.dumps(retry_payload, ensure_ascii=False, indent=2),
                    trusted_identifiers=(current_date,),
                )
            except JsonParseError as exc:
                raise AgentTurnRouteContractError(
                    [JSON_OBJECT_CONTRACT_FAILURE]
                ) from exc
        message = raw.get("message")
        if not isinstance(message, str) or not message.strip():
            raise AgentTurnRouteContractError(["message must be a non-empty string"])
        return message.strip()


def build_conversation_replier(
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ConversationReplierPort:
    resolved_provider, resolved_model = resolve_llm_selection(provider, model)
    api_key = api_key_from_env(provider=resolved_provider)
    if not api_key:
        raise RuntimeError(f"Set {provider_api_key_env(resolved_provider)}.")
    prompt_path = Path(
        os.environ.get(
            "CONVERSATION_REPLY_SYSTEM_PROMPT",
            str(DEFAULT_CONVERSATION_REPLY_PROMPT),
        )
    )
    return ChatCompletionsConversationReplier(
        ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=api_key,
                model=resolved_model,
                temperature=None,
                timeout_seconds=int_env("CONVERSATION_REPLY_LLM_TIMEOUT_SECONDS", 180),
                max_tokens=optional_int_env("CONVERSATION_REPLY_LLM_MAX_TOKENS"),
                json_mode=True,
                provider=resolved_provider,
            )
        ),
        system_prompt=prompt_path.read_text(encoding="utf-8"),
    )


def _current_date(now: datetime | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    return instant.astimezone(PRODUCT_TIMEZONE).date().isoformat()
