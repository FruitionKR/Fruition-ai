import json
import os
from pathlib import Path

from app.core.ai_markdown_sanitizer import sanitize_ai_markdown
from app.core.llm_env import api_key_from_env, int_env, optional_int_env, provider_api_key_env, resolve_llm_selection
from app.core.pipeline_control import PipelineRunCancelledError
from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute
from app.modules.wiki_generation.infrastructure.chat_completions_llm import ChatClientConfig, ChatCompletionsJsonClient
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError

DEFAULT_TASK_CLARIFICATION_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "task_clarification.system.md"
FALLBACK_QUESTION = "작업에 필요한 내용을 조금 더 알려 주세요."
RECENT_MESSAGE_LIMIT = 6
MAX_QUESTION_CHARS = 300


class ChatCompletionsTaskClarifier:
    """작업 탭에서 진행에 꼭 필요한 정보가 빠졌을 때 되물을 질문을 쓴다."""

    def __init__(self, client: ChatCompletionsJsonClient, system_prompt: str) -> None:
        self._client = client
        self._system_prompt = system_prompt

    def ask(self, request: AgentTurnRequest, route: AgentTurnRoute) -> str:
        conversation = request.conversation_context
        payload = {
            "message": request.message,
            "recent_messages": [
                {"role": message.role, "content": message.content}
                for message in (conversation.recent_messages[-RECENT_MESSAGE_LIMIT:] if conversation else ())
            ],
            "has_open_document": bool(request.active_markdown_context and request.active_markdown_context.markdown.strip()),
            "available_skills": [skill.name for skill in request.available_skills],
            "router_reason": route.reason,
        }
        try:
            raw = self._client.complete_json(self._system_prompt, json.dumps(payload, ensure_ascii=False))
        except PipelineRunCancelledError:
            raise
        except (JsonParseError, RuntimeError):
            # 되묻기는 기본 질문으로도 충분해 LLM 장애로 턴을 실패시키지 않는다. 취소·시한 초과는 그대로 올린다.
            return FALLBACK_QUESTION
        question = raw.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
            return FALLBACK_QUESTION
        return sanitize_ai_markdown(question.strip())


def build_task_clarifier(*, provider: str | None = None, model: str | None = None) -> ChatCompletionsTaskClarifier:
    resolved_provider, resolved_model = resolve_llm_selection(provider, model)
    api_key = api_key_from_env(provider=resolved_provider)
    if not api_key:
        raise RuntimeError(f"Set {provider_api_key_env(resolved_provider)}.")
    prompt_path = Path(os.environ.get("TASK_CLARIFICATION_SYSTEM_PROMPT", str(DEFAULT_TASK_CLARIFICATION_PROMPT)))
    return ChatCompletionsTaskClarifier(
        ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=api_key,
                model=resolved_model,
                temperature=None,
                timeout_seconds=int_env("TASK_CLARIFICATION_LLM_TIMEOUT_SECONDS", 60),
                max_tokens=optional_int_env("TASK_CLARIFICATION_LLM_MAX_TOKENS"),
                json_mode=True,
                provider=resolved_provider,
            )
        ),
        system_prompt=prompt_path.read_text(encoding="utf-8"),
    )
