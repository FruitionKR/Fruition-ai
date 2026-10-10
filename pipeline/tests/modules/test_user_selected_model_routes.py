import pytest
from fastapi.testclient import TestClient

import api
from app.modules.meeting_notes.interfaces.http import dependencies as meeting_deps
from app.modules.meeting_notes.interfaces.http import routes as meeting_routes
from app.modules.wiki_schema.infrastructure import chat_completions_schema_organizer as organizer_module
from app.modules.wiki_schema.interfaces.http import dependencies as schema_deps

HEADERS = {"X-Internal-Token": "test-internal"}
CLAUDE = {"provider": "claude", "model": "claude-sonnet-5"}


class RecordingClient:
    configs = []

    def __init__(self, config):
        self.configs.append(config)

    def complete_json(self, system_prompt, user_prompt):
        return {}


@pytest.fixture
def http(monkeypatch):
    RecordingClient.configs = []
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "test-internal")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-key")
    monkeypatch.setattr(meeting_routes, "authorize_speech", lambda *args: None)
    monkeypatch.setattr(meeting_deps, "ChatCompletionsJsonClient", RecordingClient)
    monkeypatch.setattr(organizer_module, "ChatCompletionsJsonClient", RecordingClient)
    monkeypatch.setattr(schema_deps, "get_wiki_schema_repository", lambda: type("Repo", (), {"save": lambda self, record: record})())
    return TestClient(api.app, raise_server_exceptions=False)


MEETING = ("/meeting-notes/preview", {"workspace_id": "w", "user_id": "u", "segments": [{"id": "s1", "text": "기록"}]})
PREVIEW = ("/wiki-schema/preview", {"raw_markdown": "# 스키마"})
DRAFT = ("/wiki-schema/drafts", {"raw_markdown": "# 스키마", "workspace_id": "w", "user_id": "u"})


@pytest.mark.parametrize("path,body", (MEETING, PREVIEW, DRAFT))
def test_request_model_reaches_chat_client_config(http, path, body):
    response = http.post(path, json={**body, **CLAUDE}, headers=HEADERS)
    assert response.status_code != 422, response.text
    assert RecordingClient.configs, response.text
    config = RecordingClient.configs[0]
    assert (config.provider, config.model, config.api_key) == ("claude", "claude-sonnet-5", "claude-key")


@pytest.mark.parametrize("path,body", (MEETING, PREVIEW, DRAFT))
@pytest.mark.parametrize(
    "selection",
    ({}, {"provider": "claude"}, {"model": "claude-sonnet-5"}, {"provider": "claude", "model": "gpt-6-luna"}),
)
def test_missing_or_unsupported_selection_is_rejected(http, path, body, selection):
    assert http.post(path, json={**body, **selection}, headers=HEADERS).status_code == 422
    assert not RecordingClient.configs


@pytest.mark.parametrize("path,body", (MEETING, PREVIEW, DRAFT))
def test_missing_provider_key_is_503_naming_the_key(http, monkeypatch, path, body):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    response = http.post(path, json={**body, **CLAUDE}, headers=HEADERS)
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.text
