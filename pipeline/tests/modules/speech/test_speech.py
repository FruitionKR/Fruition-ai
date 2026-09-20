import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import api
from app.modules.speech.domain.errors import InvalidAudioError, SpeechUnavailableError
from app.modules.speech.infrastructure import openai_speech
from app.modules.speech.interfaces.http import routes

SCOPE = {"workspace_id": "workspace", "user_id": "user"}
HEADERS = {"X-Internal-Token": "test-internal"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "test-internal")
    monkeypatch.setattr(routes, "authorize_speech", lambda *args: None)
    speech = AsyncMock()
    speech.transcribe.return_value = "문서를 찾아줘"
    speech.synthesize.return_value = b"mp3-audio"
    api.app.dependency_overrides[routes.get_speech] = lambda: speech
    yield TestClient(api.app), speech
    api.app.dependency_overrides.pop(routes.get_speech, None)


def test_transcription_requires_auth_and_valid_audio(client):
    http, speech = client
    assert http.post("/speech/transcriptions", content=b"audio").status_code == 401
    url = "/speech/transcriptions"
    assert (
        http.post(url, params=SCOPE, headers=HEADERS, content=b"audio").status_code
        == 415
    )
    headers = {**HEADERS, "Content-Type": "audio/webm"}
    assert http.post(url, params=SCOPE, headers=headers, content=b"").status_code == 422
    assert (
        http.post(
            url, params=SCOPE, headers=headers, content=b"x" * (24 * 1024 * 1024 + 1)
        ).status_code
        == 413
    )
    response = http.post(url, params=SCOPE, headers=headers, content=b"audio")
    assert response.json() == {"text": "문서를 찾아줘"}
    speech.transcribe.assert_awaited_once_with(b"audio", "audio/webm")


@pytest.mark.parametrize(
    "action",
    ["markdown_edit", "markdown_create", "conversation_reply", "clarify", "reject"],
)
def test_only_query_answers_can_be_spoken(client, action):
    http, speech = client
    response = http.post(
        "/speech/synthesis",
        headers=HEADERS,
        json={**SCOPE, "action": action, "answer": "편집했습니다."},
    )
    assert response.status_code == 422
    speech.synthesize.assert_not_awaited()


def test_query_speech_and_failure_keep_text_separate(client):
    http, speech = client
    payload = {**SCOPE, "action": "chat_answer", "answer": "검색 결과입니다."}
    response = http.post("/speech/synthesis", headers=HEADERS, json=payload)
    assert response.content == b"mp3-audio"
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["cache-control"] == "no-store"
    speech.synthesize.side_effect = SpeechUnavailableError("private-provider-detail")
    response = http.post("/speech/synthesis", headers=HEADERS, json=payload)
    assert response.status_code == 502
    assert "private-provider-detail" not in response.text


def test_permission_denial_precedes_provider_call(client, monkeypatch):
    http, speech = client

    def forbidden(*args):
        raise HTTPException(403, "forbidden")

    monkeypatch.setattr(routes, "authorize_speech", forbidden)
    assert (
        http.post(
            "/speech/synthesis",
            headers=HEADERS,
            json={
                **SCOPE,
                "action": "chat_answer",
                "answer": "test",
            },
        ).status_code
        == 403
    )
    speech.synthesize.assert_not_awaited()


def test_live_websocket_rejects_missing_internal_token(client):
    http, _ = client
    with (
        pytest.raises(WebSocketDisconnect),
        http.websocket_connect("/speech/transcriptions/live?workspace_id=w&user_id=u"),
    ):
        pytest.fail("unauthorized socket accepted")


def test_live_websocket_finishes_and_closes(client, monkeypatch):
    http, _ = client

    class FakeSpeech:
        async def transcribe_live(self, packets):
            yield {"type": "ready"}
            async for packet in packets:
                if packet == "finish":
                    yield {"type": "finished"}
                    return

    monkeypatch.setattr(routes, "get_speech", lambda: FakeSpeech())
    with http.websocket_connect(
        "/speech/transcriptions/live?workspace_id=w&user_id=u", headers=HEADERS
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"\0\0" * 2400)
        ws.send_json({"type": "finish"})
        assert ws.receive_json()["type"] == "finished"


def test_provider_http_contract_and_redacted_errors(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("transcriptions"):
            return httpx.Response(200, json={"text": " 전사 "})
        return httpx.Response(401, text="private-provider-key")

    original = httpx.AsyncClient
    monkeypatch.setattr(
        openai_speech.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler)),
    )

    async def run():
        provider = openai_speech.OpenAISpeech("fake-key")
        assert await provider.transcribe(b"audio", "audio/webm") == "전사"
        with pytest.raises(SpeechUnavailableError, match="음성을 생성"):
            await provider.synthesize("답변")

    asyncio.run(run())
    assert b"gpt-transcribe" in requests[0].content
    assert json.loads(requests[1].content)["model"] == "gpt-4o-mini-tts"


def test_realtime_drains_last_segment_and_preserves_order(monkeypatch):
    class Upstream:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.commits = 0
            self.closed = False

        async def send(self, raw):
            event = json.loads(raw)
            if event["type"] == "session.update":
                assert event["session"]["audio"]["input"]["turn_detection"] is None
                await self.queue.put({"type": "session.updated"})
            if event["type"] == "input_audio_buffer.commit":
                self.commits += 1
                item = f"item_{self.commits}"
                await self.queue.put(
                    {
                        "type": "input_audio_buffer.committed",
                        "item_id": item,
                        "previous_item_id": "item_1" if self.commits == 2 else None,
                    }
                )
                if self.commits == 2:
                    # 완료 이벤트는 발화 순서대로 오지 않을 수 있다.
                    for item in ["item_2", "item_1"]:
                        await self.queue.put(
                            {
                                "type": "conversation.item.input_audio_transcription.completed",
                                "item_id": item,
                                "transcript": item,
                            }
                        )

        async def recv(self):
            return json.dumps(await self.queue.get())

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.recv()

    async def run():
        upstream = Upstream()

        @asynccontextmanager
        async def connect(*args, **kwargs):
            try:
                yield upstream
            finally:
                upstream.closed = True

        monkeypatch.setattr(openai_speech, "connect", connect)

        async def audio():
            yield b"\0" * 4800
            yield "commit"
            yield b"\0" * 4800
            yield "finish"

        events = [
            event
            async for event in openai_speech.OpenAISpeech("fake").transcribe_live(
                audio()
            )
        ]
        assert events[-1] == {"type": "finished", "segment_count": 2}
        finals = [e for e in events if e["type"] == "completed"]
        assert finals[0]["previous_segment_id"] == "item_1"
        assert upstream.closed

        async def invalid():
            yield b"odd"

        with pytest.raises(InvalidAudioError):
            _ = [
                e
                async for e in openai_speech.OpenAISpeech("fake").transcribe_live(
                    invalid()
                )
            ]
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(run())
