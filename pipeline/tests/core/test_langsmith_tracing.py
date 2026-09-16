import os

from app.core.langsmith_tracing import (
    disable_unconfigured_langsmith_tracing,
    langsmith_tracing_enabled,
)


def test_langsmith_tracing_requires_api_key(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)

    assert langsmith_tracing_enabled() is False


def test_langsmith_tracing_is_enabled_with_flag_and_api_key(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")

    assert langsmith_tracing_enabled() is True


def test_unconfigured_tracing_flags_are_disabled_for_studio(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)

    disable_unconfigured_langsmith_tracing()

    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
