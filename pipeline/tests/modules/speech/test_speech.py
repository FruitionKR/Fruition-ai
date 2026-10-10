import asyncio
import base64
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
    speech.synthesize.return_value = b"wav-audio"
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
    assert response.content == b"wav-audio"
    assert response.headers["content-type"] == "audio/wav"
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

    asyncio.run(run())
    assert b"gpt-transcribe" in requests[0].content


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


def _ledger_rows(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.modules.model_usage.infrastructure import usage_ledger

    rows = {}

    def execute(sql, params):
        if sql.lstrip().startswith("INSERT"):
            rows[params[0]] = {"model": params[6], "status": "started"}
        else:
            rows[params[-1]].update(
                status=params[0],
                input_tokens=params[2],
                output_tokens=params[3],
                audio_seconds=params[7],
                input_characters=params[8],
            )

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(usage_ledger.database, "connect_ai", connect)
    return usage_ledger.usage_scope({"run_id": "req", **SCOPE}), rows


def test_file_transcription_is_recorded(monkeypatch):
    scope, rows = _ledger_rows(monkeypatch)

    def handler(request):
        return httpx.Response(
            200, json={"text": "전사", "usage": {"type": "duration", "seconds": 4}}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        openai_speech.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler)),
    )

    async def run():
        with scope:
            provider = openai_speech.OpenAISpeech("fake-key")
            await provider.transcribe(b"audio", "audio/webm")

    asyncio.run(run())
    (transcribe,) = rows.values()
    assert transcribe["model"] == "gpt-transcribe" and transcribe["status"] == "succeeded"
    assert transcribe["audio_seconds"] == 4.0


def _fake_realtime_tts(monkeypatch, response_events):
    """session.updated 이후 response.create를 받으면 response_events를 돌려주는 가짜 upstream."""
    sent = []
    urls = []

    class Upstream:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def send(self, raw):
            event = json.loads(raw)
            sent.append(event)
            if event["type"] == "session.update":
                await self.queue.put({"type": "session.updated"})
            if event["type"] == "response.create":
                for item in response_events:
                    await self.queue.put(item)

        async def recv(self):
            return json.dumps(await self.queue.get())

    @asynccontextmanager
    async def connect(url, **kwargs):
        urls.append(url)
        yield Upstream()

    monkeypatch.setattr(openai_speech, "connect", connect)
    return sent, urls


def _delta(raw):
    return {"type": "response.output_audio.delta", "delta": base64.b64encode(raw).decode()}


def test_tts_uses_realtime_and_returns_wav_with_usage(monkeypatch):
    import io
    import wave

    scope, rows = _ledger_rows(monkeypatch)
    pcm = b"\x01\x00" * 2400
    usage = {"input_tokens": 12, "output_tokens": 90,
             "input_token_details": {"cached_tokens": 4}}
    sent, urls = _fake_realtime_tts(
        monkeypatch,
        [_delta(pcm[:3000]), _delta(pcm[3000:]),
         {"type": "response.done", "response": {"usage": usage}}],
    )

    async def run():
        with scope:
            return await openai_speech.OpenAISpeech("fake").synthesize("답변입니다")

    wav_bytes = asyncio.run(run())
    assert urls == ["wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini"]
    assert [e["type"] for e in sent] == [
        "session.update", "conversation.item.create", "response.create"]
    session = sent[0]["session"]
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["output"] == {
        "format": {"type": "audio/pcm", "rate": 24000}, "voice": "marin"}
    assert sent[1]["item"]["content"] == [{"type": "input_text", "text": "답변입니다"}]
    assert sent[2]["response"]["max_output_tokens"] == "inf"
    assert wav_bytes[:4] == b"RIFF"
    with wave.open(io.BytesIO(wav_bytes)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000, 1, 2)
        assert wav.readframes(wav.getnframes()) == pcm
    (row,) = rows.values()
    assert row["model"] == "gpt-realtime-2.1-mini" and row["status"] == "succeeded"
    assert row["input_characters"] == 5
    assert (row["input_tokens"], row["output_tokens"]) == (12, 90)


@pytest.mark.parametrize(
    "events",
    [
        [{"type": "error", "error": {"message": "private"}}],
        [{"type": "response.done", "response": {"usage": {}}}],
    ],
)
def test_tts_error_event_and_empty_audio_are_unavailable(monkeypatch, events):
    scope, rows = _ledger_rows(monkeypatch)
    _fake_realtime_tts(monkeypatch, events)

    async def run():
        with scope:
            await openai_speech.OpenAISpeech("fake").synthesize("답변")

    with pytest.raises(SpeechUnavailableError, match="음성을 생성"):
        asyncio.run(run())
    (row,) = rows.values()
    assert row["status"] == "failed"


def test_live_transcription_records_each_committed_segment(monkeypatch):
    scope, rows = _ledger_rows(monkeypatch)

    class Upstream:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.commits = 0

        async def send(self, raw):
            event = json.loads(raw)
            if event["type"] == "session.update":
                await self.queue.put({"type": "session.updated"})
            if event["type"] == "input_audio_buffer.commit":
                self.commits += 1
                item = f"item_{self.commits}"
                await self.queue.put(
                    {"type": "input_audio_buffer.committed", "item_id": item}
                )
                if self.commits == 1:
                    await self.queue.put(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": item,
                            "transcript": "첫 구간",
                            "usage": {"input_tokens": 9, "output_tokens": 3},
                        }
                    )
                else:
                    await self.queue.put({"type": "error"})

        async def recv(self):
            return json.dumps(await self.queue.get())

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.recv()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield Upstream()

    monkeypatch.setattr(openai_speech, "connect", connect)

    async def audio():
        yield b"\0" * 4800
        yield "commit"
        yield b"\0" * 9600
        yield "commit"
        await asyncio.sleep(1)

    async def run():
        with scope:
            with pytest.raises(SpeechUnavailableError):
                async for _ in openai_speech.OpenAISpeech("fake").transcribe_live(
                    audio()
                ):
                    pass

    asyncio.run(run())
    first, second = rows.values()
    assert first == {
        "model": "gpt-live-transcribe",
        "status": "succeeded",
        "input_tokens": 9,
        "output_tokens": 3,
        "audio_seconds": 0.1,
        "input_characters": None,
    }
    # 전사를 받지 못한 구간도 보낸 오디오 길이와 함께 남는다.
    assert second["status"] == "failed" and second["audio_seconds"] == 0.2


def test_speech_routes_attribute_calls_to_request_id(client, monkeypatch):
    from app.modules.model_usage.infrastructure import usage_ledger

    http, speech = client
    actors = []

    async def transcribe(*args):
        actors.append(usage_ledger._actor.get())
        return "전사"

    async def synthesize(*args):
        actors.append(usage_ledger._actor.get())
        return b"wav"

    class LiveSpeech:
        async def transcribe_live(self, packets):
            actors.append(usage_ledger._actor.get())
            yield {"type": "finished"}

    speech.transcribe.side_effect = transcribe
    speech.synthesize.side_effect = synthesize
    monkeypatch.setattr(routes, "get_speech", lambda: LiveSpeech())
    http.post(
        "/speech/transcriptions?workspace_id=w&user_id=u",
        content=b"audio",
        headers={**HEADERS, "Content-Type": "audio/webm", "X-Request-Id": "req-stt"},
    )
    http.post(
        "/speech/synthesis",
        json={"workspace_id": "w", "user_id": "u", "action": "chat_answer", "answer": "답변"},
        headers={**HEADERS, "X-Request-Id": "req-tts"},
    )
    with http.websocket_connect(
        "/speech/transcriptions/live?workspace_id=w&user_id=u",
        headers={**HEADERS, "X-Request-Id": "req-live"},
    ) as ws:
        assert ws.receive_json()["type"] == "finished"
    assert actors == [
        {"run_id": run_id, "workspace_id": "w", "user_id": "u", "kind": kind}
        for run_id, kind in [
            ("req-stt", "speech_transcription"),
            ("req-tts", "speech_synthesis"),
            ("req-live", "speech_live_transcription"),
        ]
    ]
