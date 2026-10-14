import asyncio
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.core.pipeline_control import (
    DeadlineExceededError,
    ensure_task_active,
    remaining_seconds,
    request_deadline,
)
from app.modules.speech.domain.errors import SpeechUnavailableError
from app.modules.speech.infrastructure import openai_speech
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)
from app.modules.wiki_schema.application.create_schema_draft import CreateSchemaDraftUseCase
from app.modules.wiki_schema.domain.entities import SchemaFragments
from app.modules.wiki_schema.application.ports import SchemaOrganizerCandidate
from app.modules.wiki_schema.interfaces.http.routes import create_wiki_schema_draft, preview_wiki_schema
from app.modules.wiki_schema.interfaces.http.schemas import CreateWikiSchemaDraftRequest, WikiSchemaPreviewRequest
from fastapi import HTTPException


def test_deadline_is_inactive_by_default_and_nested_scope_keeps_the_earlier_one() -> None:
    assert remaining_seconds() is None
    ensure_task_active()
    with request_deadline(10):
        with request_deadline(100):
            assert 0 < remaining_seconds() <= 10
        with request_deadline(-1), pytest.raises(DeadlineExceededError):
            ensure_task_active()
    assert remaining_seconds() is None


def _client() -> ChatCompletionsJsonClient:
    return ChatCompletionsJsonClient(ChatClientConfig(api_key="fake", model="gpt-5-nano", provider="openai"))


def test_llm_call_is_refused_after_deadline_without_reaching_the_provider() -> None:
    client = _client()
    client._model = MagicMock()
    with request_deadline(-1), pytest.raises(DeadlineExceededError):
        client.complete_text("system", "user")
    client._model.invoke.assert_not_called()


def test_llm_call_under_deadline_caps_provider_timeout_and_disables_internal_retries() -> None:
    client = _client()
    client._model = MagicMock()
    capped = MagicMock()
    capped.invoke.return_value = MagicMock(text="ok")
    with request_deadline(30), patch.object(client, "_build_model", return_value=capped) as build:
        assert client.complete_text("system", "user") == "ok"
    timeout, retries = build.call_args.args
    assert 0 < timeout <= 30 and retries == 0
    client._model.invoke.assert_not_called()


def test_provider_timeout_during_call_surfaces_as_deadline_error() -> None:
    client = _client()
    failing = MagicMock()

    def times_out_at_deadline(messages):
        time.sleep(0.06)
        raise TimeoutError("read timed out")

    failing.invoke.side_effect = times_out_at_deadline
    with request_deadline(0.05), patch.object(client, "_build_model", return_value=failing):
        with pytest.raises(DeadlineExceededError):
            client.complete_text("system", "user")


class _Organizer:
    def organize(self, raw_markdown):
        return SchemaOrganizerCandidate(fragments=SchemaFragments(global_markdown="- 한국어로 작성한다."),
                                        blocked_candidates=[], unclear_items=[])


def test_schema_draft_is_not_saved_after_deadline() -> None:
    repository = MagicMock()
    use_case = CreateSchemaDraftUseCase(_Organizer(), repository)
    with request_deadline(-1), pytest.raises(DeadlineExceededError):
        use_case.execute("- 한국어로 작성한다.", "ws", "user")
    repository.save.assert_not_called()


@pytest.mark.parametrize("route", ["preview", "drafts"])
def test_schema_routes_return_504_when_deadline_expires(route) -> None:
    failing = MagicMock()
    failing.execute.side_effect = DeadlineExceededError("Request deadline exceeded.")
    if route == "preview":
        builder, call = "build_organize_schema_use_case", lambda: preview_wiki_schema(
            WikiSchemaPreviewRequest(raw_markdown="x", provider="openai", model="gpt-5-nano"))
    else:
        builder, call = "build_create_schema_draft_use_case", lambda: create_wiki_schema_draft(
            CreateWikiSchemaDraftRequest(raw_markdown="x", workspace_id="ws", user_id="user",
                                         provider="openai", model="gpt-5-nano"))
    with patch(f"app.modules.wiki_schema.interfaces.http.routes.{builder}", return_value=failing):
        with pytest.raises(HTTPException) as caught:
            call()
    assert caught.value.status_code == 504 and caught.value.detail == "deadline_exceeded"


def _slow_transcription(monkeypatch, timeouts):
    async def slow(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"text": "늦은 전사"})

    original = httpx.AsyncClient

    def client(**kwargs):
        timeouts.append(kwargs["timeout"])
        return original(transport=httpx.MockTransport(slow))

    monkeypatch.setattr(openai_speech.httpx, "AsyncClient", client)


def test_speech_transcription_gives_up_at_its_deadline(monkeypatch) -> None:
    timeouts = []
    _slow_transcription(monkeypatch, timeouts)

    async def run():
        # asyncio.timeout은 float도 받으므로 짧은 상한으로 상한 적용 여부만 본다.
        await openai_speech.OpenAISpeech("fake-key").transcribe(b"audio", "audio/webm", 0.05)

    with pytest.raises(SpeechUnavailableError):
        asyncio.run(run())
    assert timeouts == [0.05]


def test_speech_transcription_without_deadline_is_not_capped(monkeypatch) -> None:
    timeouts = []
    _slow_transcription(monkeypatch, timeouts)

    text = asyncio.run(openai_speech.OpenAISpeech("fake-key").transcribe(b"audio", "audio/webm"))
    assert text == "늦은 전사"
    assert timeouts == [openai_speech.TRANSCRIBE_DEFAULT_TIMEOUT_SECONDS]
