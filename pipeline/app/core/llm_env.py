from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass


SUPPORTED_LLM_PROVIDERS = ("openai", "gemini", "claude")
# 모델 목록은 백엔드 카탈로그가 관리한다. 여기 값은 호출자가 모델을 고르지 않을 때의 기본값이다.
DEFAULT_LLM_MODELS = {
    "openai": "gpt-6-luna",
    "gemini": "gemini-3.5-flash-lite",
    "claude": "claude-sonnet-5",
}
_MODEL_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
_PROVIDER_MODEL_PREFIXES = {
    "openai": ("gpt-", "chatgpt-", "o1", "o3", "o4"),
    "gemini": ("gemini-",),
    "claude": ("claude-",),
}
# Chat Completions·Gemini·Messages API로 텍스트 JSON을 받을 수 없는 계열이다.
_NON_TEXT_MODEL_MARKERS = (
    "image", "tts", "audio", "realtime", "transcribe", "whisper", "embedding",
    "search", "live", "instruct", "omni", "computer-use", "robotics",
)
# OpenAI의 pro·codex 계열은 Responses API 전용이다. Gemini pro는 일반 텍스트 모델이다.
_OPENAI_NON_CHAT_MODEL_MARKERS = ("codex", "-pro")
_OPENAI_REASONING_MODEL = re.compile(r"(gpt-[5-9]|o\d)")
_GEMINI_THINKING_LEVEL_MODEL = re.compile(r"gemini-([3-9]|\d{2,})")
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
    if not _is_text_model(resolved_provider, resolved_model):
        raise ValueError(
            f"Unsupported model for {resolved_provider}: {resolved_model}. "
            f"Expected a {resolved_provider} text model such as {DEFAULT_LLM_MODELS[resolved_provider]}"
        )
    return resolved_provider, resolved_model


def _is_text_model(provider: str, model: str) -> bool:
    if not _MODEL_NAME_PATTERN.fullmatch(model):
        return False
    if not model.startswith(_PROVIDER_MODEL_PREFIXES[provider]):
        return False
    markers = _NON_TEXT_MODEL_MARKERS
    if provider == "openai":
        markers += _OPENAI_NON_CHAT_MODEL_MARKERS
    return not any(marker in model for marker in markers)


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
    # 추론 수준 파라미터는 지원하는 계열에만 보낸다. 나머지 모델은 값을 받으면 400을 낸다.
    if resolved_provider == "openai":
        if _OPENAI_REASONING_MODEL.match(resolved_model) and "chat-latest" not in resolved_model:
            return {"reasoning_effort": "medium"}
        return {}
    if resolved_provider == "gemini":
        if _GEMINI_THINKING_LEVEL_MODEL.match(resolved_model):
            return {"reasoning_effort": "low"}
        return {}
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
