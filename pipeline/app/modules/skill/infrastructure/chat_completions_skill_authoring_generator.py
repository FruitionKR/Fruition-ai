import json
import os
from pathlib import Path

from app.core.llm_env import (
    api_key_from_env,
    int_env,
    provider_api_key_env,
    resolve_llm_selection,
)
from app.modules.skill.domain.entities import SkillAuthoringMode, SkillAuthoringReference
from app.modules.skill.domain.reference_template import extract_markdown_structure
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)


DEFAULT_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "skill_authoring.system.md"
DEFAULT_CLASSIFIER_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "skill_intent_classifier.system.md"


class ChatCompletionsSkillAuthoringGenerator:
    def __init__(
        self,
        client: ChatCompletionsJsonClient,
        system_prompt: str,
        classifier_prompt: str,
    ) -> None:
        self._client = client
        self._system_prompt = system_prompt
        self._classifier_prompt = classifier_prompt

    def classify(
        self,
        instruction: str,
        references: tuple[SkillAuthoringReference, ...],
        *,
        requested_description: str | None,
    ) -> dict[str, object]:
        return self._complete_intent(
            self._classifier_prompt,
            instruction,
            references,
            requested_description,
        )

    def generate(
        self,
        instruction: str,
        references: tuple[SkillAuthoringReference, ...],
        *,
        allow_clarification: bool,
        authoring_mode: SkillAuthoringMode,
        requested_name: str | None,
        requested_description: str | None = None,
        reference_mode: str,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "instruction": instruction,
            "authoring_mode": authoring_mode,
            "requested_name": requested_name,
            "requested_description": requested_description,
            "reference_mode": reference_mode,
            "interaction_mode": "multi_turn" if allow_clarification else "single_turn",
            "references": [
                {
                    "markdown_structure": extract_markdown_structure(reference.markdown),
                }
                for reference in references
            ],
        }
        result = self._complete(payload)
        if allow_clarification or result.get("status") != "clarification_required":
            return result
        payload["contract_failures"] = [
            "single_turn authoring must create a conservative editable proposal instead of asking a question"
        ]
        return self._complete(payload)

    def _complete_intent(
        self,
        system_prompt: str,
        instruction: str,
        references: tuple[SkillAuthoringReference, ...],
        requested_description: str | None,
    ) -> dict[str, object]:
        return self._client.complete_json(
            system_prompt,
            json.dumps(
                {
                    "instruction": instruction,
                    "requested_description": requested_description,
                    "references": [
                        {"markdown_structure": extract_markdown_structure(reference.markdown)}
                        for reference in references
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    def _complete(self, payload: dict[str, object]) -> dict[str, object]:
        return self._client.complete_json(
            self._system_prompt,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )


def build_skill_authoring_generator(
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ChatCompletionsSkillAuthoringGenerator:
    resolved_provider, resolved_model = resolve_llm_selection(provider, model)
    api_key = api_key_from_env(
        provider=resolved_provider,
    )
    if not api_key or not resolved_model:
        raise RuntimeError(f"Set {provider_api_key_env(resolved_provider)} and pass a model.")
    prompt_path = Path(os.environ.get("SKILL_AUTHORING_SYSTEM_PROMPT", str(DEFAULT_PROMPT)))
    classifier_prompt_path = Path(
        os.environ.get("SKILL_INTENT_CLASSIFIER_SYSTEM_PROMPT", str(DEFAULT_CLASSIFIER_PROMPT))
    )
    return ChatCompletionsSkillAuthoringGenerator(
        ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=api_key,
                model=resolved_model,
                temperature=None if resolved_provider == "claude" else 0.0,
                timeout_seconds=int_env("SKILL_AUTHORING_LLM_TIMEOUT_SECONDS", 180),
                max_tokens=None,
                json_mode=True,
                provider=resolved_provider,
            )
        ),
        prompt_path.read_text(encoding="utf-8"),
        classifier_prompt_path.read_text(encoding="utf-8"),
    )
