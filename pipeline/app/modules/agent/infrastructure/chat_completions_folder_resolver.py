import json
import os
from pathlib import Path

from app.core.llm_env import api_key_from_env, int_env, optional_int_env, provider_api_key_env, resolve_llm_selection
from app.modules.agent.domain.entities import AgentTurnRequest
from app.modules.agent.domain.folder_lookup import (
    FOLDER_REFERENCE_FAILED_MESSAGE,
    MAX_FOLDER_CANDIDATES,
    FolderEntry,
    FolderLookupUnavailableError,
)
from app.modules.wiki_generation.infrastructure.chat_completions_llm import ChatClientConfig, ChatCompletionsJsonClient
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError

DEFAULT_FOLDER_LOOKUP_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "agent_folder_lookup.system.md"
RECENT_MESSAGE_LIMIT = 6


class ChatCompletionsFolderResolver:
    """메시지가 지목한 폴더를 LLM이 고른다. 폴더 id 대신 짧은 키(f1, f2…)를 보여 줘 없는 id를 만들지 않게 한다."""

    def __init__(self, client: ChatCompletionsJsonClient, system_prompt: str) -> None:
        self._client = client
        self._system_prompt = system_prompt

    def resolve(self, request: AgentTurnRequest, folders: list[FolderEntry]) -> list[str]:
        # ponytail: 폴더가 MAX_FOLDER_CANDIDATES개를 넘으면 뒤쪽 폴더는 고를 수 없다. 많아지면 이름 검색으로 후보를 먼저 줄인다.
        keyed = {f"f{index}": folder for index, folder in enumerate(folders[:MAX_FOLDER_CANDIDATES], start=1)}
        conversation = request.conversation_context
        payload = {
            "message": request.message,
            "recent_messages": [
                {"role": message.role, "content": message.content}
                for message in (conversation.recent_messages[-RECENT_MESSAGE_LIMIT:] if conversation else ())
            ],
            "folders": [{"key": key, "name": folder.name, "path": " / ".join(folder.path)} for key, folder in keyed.items()],
        }
        try:
            raw = self._client.complete_json(self._system_prompt, json.dumps(payload, ensure_ascii=False))
        except JsonParseError as exc:
            raise FolderLookupUnavailableError(FOLDER_REFERENCE_FAILED_MESSAGE) from exc
        keys = raw.get("folders")
        if not isinstance(keys, list):
            raise FolderLookupUnavailableError(FOLDER_REFERENCE_FAILED_MESSAGE)
        # 목록에 없는 키는 버린다.
        return [keyed[key].id for key in keys if isinstance(key, str) and key in keyed]


def build_folder_reference_resolver(*, provider: str | None = None, model: str | None = None) -> ChatCompletionsFolderResolver:
    resolved_provider, resolved_model = resolve_llm_selection(provider, model)
    api_key = api_key_from_env(provider=resolved_provider)
    if not api_key:
        raise RuntimeError(f"Set {provider_api_key_env(resolved_provider)}.")
    prompt_path = Path(os.environ.get("AGENT_FOLDER_LOOKUP_SYSTEM_PROMPT", str(DEFAULT_FOLDER_LOOKUP_PROMPT)))
    return ChatCompletionsFolderResolver(
        ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=api_key,
                model=resolved_model,
                temperature=None,
                timeout_seconds=int_env("AGENT_FOLDER_LOOKUP_LLM_TIMEOUT_SECONDS", 60),
                max_tokens=optional_int_env("AGENT_FOLDER_LOOKUP_LLM_MAX_TOKENS"),
                json_mode=True,
                provider=resolved_provider,
            )
        ),
        system_prompt=prompt_path.read_text(encoding="utf-8"),
    )
