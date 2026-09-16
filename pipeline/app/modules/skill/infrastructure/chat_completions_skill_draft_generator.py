import json
import os
from pathlib import Path

from app.core.llm_env import (
    api_key_from_env,
    int_env,
    provider_api_key_env,
    resolve_llm_selection,
)
from app.modules.skill.domain.entities import SkillDraftSourceRun
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)


DEFAULT_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "skill_draft_proposal.system.md"


class ChatCompletionsSkillDraftGenerator:
    def __init__(self, client: ChatCompletionsJsonClient, system_prompt: str) -> None:
        self._client = client
        self._system_prompt = system_prompt

    def generate(
        self,
        source_runs: tuple[SkillDraftSourceRun, ...],
        user_directives: tuple[str, ...],
    ) -> dict[str, object]:
        return self._client.complete_json(
            self._system_prompt,
            json.dumps(
                {
                    "source_runs": [
                        {
                            "request_summary": source.request_summary,
                            "plan_summary": source.plan_summary,
                            "successful_operations": [
                                {
                                    "tool_name": operation.tool_name,
                                    "reason": operation.reason,
                                }
                                for operation in source.successful_operations
                            ],
                        }
                        for source in source_runs
                    ],
                    "user_directives": list(user_directives),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )


def build_skill_draft_generator(
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ChatCompletionsSkillDraftGenerator:
    resolved_provider, resolved_model = resolve_llm_selection(provider, model)
    api_key = api_key_from_env(
        provider=resolved_provider,
    )
    if not api_key or not resolved_model:
        raise RuntimeError(f"Set {provider_api_key_env(resolved_provider)} and pass a model.")
    prompt_path = Path(os.environ.get("SKILL_DRAFT_SYSTEM_PROMPT", str(DEFAULT_PROMPT)))
    return ChatCompletionsSkillDraftGenerator(
        ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=api_key,
                model=resolved_model,
                temperature=None,
                timeout_seconds=int_env("SKILL_DRAFT_LLM_TIMEOUT_SECONDS", 180),
                max_tokens=None,
                json_mode=True,
                provider=resolved_provider,
            )
        ),
        prompt_path.read_text(encoding="utf-8"),
    )
