import pytest

from app.core.llm_env import provider_api_key_env
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    _api_key as agent_api_key,
    build_agent_turn_router,
)
from app.modules.agent_run.infrastructure.chat_completions_plan_generator import (
    build_plan_generator,
)
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import (
    _api_key as markdown_api_key,
    build_markdown_editor,
)
from app.modules.query.infrastructure.query_chat_answer_generator import (
    _api_key as query_api_key,
    _config_from_env,
    build_query_chat_answer_generator,
)
from app.modules.skill.infrastructure.chat_completions_skill_authoring_generator import (
    build_skill_authoring_generator,
)
from app.modules.wiki_schema.infrastructure.chat_completions_schema_organizer import (
    build_schema_organizer,
)


EXPECTED = {
    "openai": ("OPENAI_API_KEY", "gpt-5-nano"),
    "gemini": ("GEMINI_API_KEY", "gemini-3.1-flash-lite"),
    "claude": ("ANTHROPIC_API_KEY", "claude-sonnet-5"),
}


@pytest.mark.parametrize("provider", EXPECTED)
def test_runtime_config_uses_fixed_provider_key_and_model(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    key_env, model = EXPECTED[provider]
    monkeypatch.setenv(key_env, "provider-key")
    monkeypatch.setenv("LLM_API_KEY", "legacy-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://legacy.example/v1")
    monkeypatch.setenv("LLM_MODEL", "legacy-model")

    assert query_api_key(provider) == "provider-key"
    config = _config_from_env(provider=provider, model=model)
    assert config.api_key == "provider-key"
    assert config.model == model
    assert config.provider == provider


@pytest.mark.parametrize(
    ("provider", "api_key_reader"),
    (
        ("openai", agent_api_key),
        ("gemini", markdown_api_key),
        ("claude", query_api_key),
    ),
)
def test_feature_readers_use_selected_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    api_key_reader,
) -> None:
    key_env, _ = EXPECTED[provider]
    monkeypatch.setenv(key_env, "provider-key")
    assert api_key_reader(provider) == "provider-key"


@pytest.mark.parametrize(
    ("provider", "builder"),
    (
        ("openai", build_agent_turn_router),
        ("gemini", build_markdown_editor),
        ("openai", build_skill_authoring_generator),
        ("claude", build_query_chat_answer_generator),
    ),
)
def test_missing_provider_api_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    builder,
) -> None:
    model = EXPECTED[provider][1]
    for key_env in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key_env, raising=False)

    with pytest.raises(RuntimeError, match=provider_api_key_env(provider)):
        builder(provider=provider, model=model)


@pytest.mark.parametrize("provider", EXPECTED)
def test_agent_plan_llm_uses_request_provider_and_model(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    key_env, model = EXPECTED[provider]
    monkeypatch.setenv(key_env, "provider-key")
    client = build_plan_generator(provider=provider, model=model)._client  # type: ignore[attr-defined]

    assert client.provider == provider
    assert client.config.model == model
    assert client.config.temperature == (None if provider == "claude" else 0.0)


def test_agent_plan_llm_rejects_unsupported_provider_or_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    with pytest.raises(ValueError, match="Unsupported provider"):
        build_plan_generator(provider="upstage", model="solar-pro2")
    with pytest.raises(ValueError, match="Unsupported model"):
        build_plan_generator(provider="openai", model="unsupported-model")
