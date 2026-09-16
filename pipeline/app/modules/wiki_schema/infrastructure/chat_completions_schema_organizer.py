from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from app.core.llm_env import api_key_from_env
from app.modules.wiki_generation.infrastructure.chat_completions_llm import ChatClientConfig, ChatCompletionsJsonClient
from app.modules.wiki_schema.application.ports import SchemaOrganizerPort
from app.modules.wiki_schema.domain.entities import SchemaFragments, SchemaOrganizerCandidate


DEFAULT_SCHEMA_ORGANIZER_PROMPT = Path(__file__).resolve().parents[4] / "prompts" / "wiki_schema_organizer.system.md"


class JsonChatClient(Protocol):
    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        ...


class ChatCompletionsSchemaOrganizer(SchemaOrganizerPort):
    def __init__(self, client: JsonChatClient, system_prompt: str) -> None:
        self._client = client
        self._system_prompt = system_prompt

    def organize(self, raw_markdown: str) -> SchemaOrganizerCandidate:
        payload = {
            "raw_markdown": raw_markdown,
            "target_sections": [
                "global_markdown",
                "query_markdown",
                "ingest_markdown",
                "edit_markdown",
                "concept_markdown",
                "template_markdown",
            ],
        }
        raw = self._client.complete_json(self._system_prompt, json.dumps(payload, ensure_ascii=False, indent=2))
        return _normalize_candidate(raw)


def build_schema_organizer() -> SchemaOrganizerPort:
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("Set OPENAI_API_KEY.")
    model = "gpt-5-nano"
    prompt_path = Path(os.environ.get("WIKI_SCHEMA_SYSTEM_PROMPT", str(DEFAULT_SCHEMA_ORGANIZER_PROMPT)))
    return ChatCompletionsSchemaOrganizer(
        ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=api_key,
                model=model,
                temperature=None,
                timeout_seconds=180,
                max_tokens=1200,
                json_mode=True,
                provider="openai",
            )
        ),
        system_prompt=prompt_path.read_text(encoding="utf-8"),
    )


def _normalize_candidate(value: dict[str, Any]) -> SchemaOrganizerCandidate:
    return SchemaOrganizerCandidate(
        fragments=SchemaFragments(
            global_markdown=_optional_text(value.get("global_markdown") or value.get("globalMarkdown")),
            query_markdown=_optional_text(value.get("query_markdown") or value.get("queryMarkdown")),
            ingest_markdown=_optional_text(value.get("ingest_markdown") or value.get("ingestMarkdown")),
            edit_markdown=_optional_text(value.get("edit_markdown") or value.get("editMarkdown")),
            concept_markdown=_optional_text(value.get("concept_markdown") or value.get("conceptMarkdown")),
            template_markdown=_optional_text(value.get("template_markdown") or value.get("templateMarkdown")),
        ),
        blocked_candidates=_string_list(value.get("blocked_candidates") or value.get("blockedCandidates")),
        unclear_items=_string_list(value.get("unclear_items") or value.get("unclearItems")),
    )


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _api_key() -> str | None:
    return api_key_from_env(
        provider="openai",
        strip=True,
    )


def _float_env(name: str, default: float) -> float:
    return float_env(name, default)


def _int_env(name: str, default: int) -> int:
    return int_env(name, default)


def _optional_int_env(name: str) -> int | None:
    return optional_int_env(name)
