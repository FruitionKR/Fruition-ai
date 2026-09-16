from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass


SUPPORTED_LLM_PROVIDERS = ("openai", "gemini", "claude")
SUPPORTED_LLM_MODELS = {
    "openai": "gpt-5-nano",
    "gemini": "gemini-3.1-flash-lite",
    "claude": "claude-sonnet-5",
}
_PROVIDER_API_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
}


@dataclass(frozen=True)
class LlmProviderDefaults:
    provider: str
    api_key_env: str
    api_key: str | None
    model: str | None


def resolve_llm_selection(provider: str | None, model: str | None) -> tuple[str, str]:
    if (provider is None) != (model is None):
        raise ValueError("provider and model must be provided together")
    if provider is None or model is None:
        raise ValueError("provider and model are required")
    resolved_provider = resolve_llm_provider(provider)
    resolved_model = model.strip()
    if resolved_model != SUPPORTED_LLM_MODELS[resolved_provider]:
        raise ValueError(
            f"Unsupported model for {resolved_provider}: {resolved_model}. "
            f"Expected {SUPPORTED_LLM_MODELS[resolved_provider]}"
        )
    return resolved_provider, resolved_model


def resolve_llm_provider(provider: str | None = None) -> str:
    resolved = (provider or "openai").strip().lower()
    if resolved not in SUPPORTED_LLM_PROVIDERS:
        supported = ", ".join(SUPPORTED_LLM_PROVIDERS)
        raise ValueError(f"Unsupported provider: {resolved}. Expected one of: {supported}")
    return resolved


def provider_api_key_env(provider: str | None = None) -> str:
    return _PROVIDER_API_KEY_ENVS[resolve_llm_provider(provider)]


def inference_profile(provider: str, model: str) -> dict[str, str]:
    resolved_provider, resolved_model = resolve_llm_selection(provider, model)
    if resolved_provider == "openai":
        return {"reasoning_effort": "medium"}
    if resolved_provider == "gemini":
        return {"reasoning_effort": "low"}
    return {}


def resolve_llm_provider_defaults(
    *,
    provider: str | None = None,
    api_key_env: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> LlmProviderDefaults:
    resolved_provider = resolve_llm_provider(provider)
    resolved_key_env = provider_api_key_env(resolved_provider)
    if api_key_env and api_key_env != resolved_key_env:
        raise ValueError(f"API key env is fixed to {resolved_key_env}")
    if model is not None:
        _, model = resolve_llm_selection(resolved_provider, model)
    resolved_key = api_key or os.environ.get(resolved_key_env)
    return LlmProviderDefaults(
        provider=resolved_provider,
        api_key_env=resolved_key_env,
        api_key=resolved_key,
        model=model,
    )

def api_key_from_env(
    *,
    provider: str | None = None,
    strip: bool = False,
) -> str | None:
    return first_env((provider_api_key_env(provider),), strip=strip)


def first_env(env_names: Iterable[str], *, strip: bool = False) -> str | None:
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return _normalize(value, strip)
    return None


def float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def optional_int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _normalize(value: str, strip: bool) -> str | None:
    if not strip:
        return value
    stripped = value.strip()
    return stripped or None
