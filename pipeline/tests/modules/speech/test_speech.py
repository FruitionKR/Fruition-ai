import asyncio
import base64
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

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


def _stream(deltas, error=None):
    def synthesize(text):
        async def gen():
            for delta in deltas:
                yield delta
            if error:
                raise error

        return gen()

    return synthesize


def _events(response):
    return [json.loads(line) for line in response.text.splitlines()]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "test-internal")
    monkeypatch.setattr(routes, "authorize_speech", lambda *args: None)
    speech = AsyncMock()
    speech.transcribe.return_value = "문서를 찾아줘"
    speech.synthesize = MagicMock(side_effect=_stream(["AAA=", "BBB="]))
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
    speech.transcribe.assert_awaited_once_with(b"audio", "audio/webm", None)
    # 채팅 호출자만 deadline_seconds를 보내고, 범위(1..600) 밖이면 거절한다.
    http.post(url, params={**SCOPE, "deadline_seconds": 100}, headers=headers, content=b"audio")
    speech.transcribe.assert_awaited_with(b"audio", "audio/webm", 100)
    for bad in (0, 601):
        assert http.post(url, params={**SCOPE, "deadline_seconds": bad}, headers=headers,
                         content=b"audio").status_code == 422


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
    speech.synthesize.assert_not_called()


def test_synthesis_streams_ndjson_then_done(client):
    http, speech = client
    payload = {**SCOPE, "action": "chat_answer", "answer": "검색 결과입니다."}
    response = http.post("/speech/synthesis", headers=HEADERS, json=payload)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["cache-control"] == "no-store"
    assert _events(response) == [
        {"type": "audio", "delta": "AAA="},
        {"type": "audio", "delta": "BBB="},
        {"type": "done"},
    ]


@pytest.mark.parametrize(
    "error,code",
    [
        (SpeechUnavailableError("private-provider-detail"), "provider_unavailable"),
        (SpeechUnavailableError("x", "provider_rate_limited"), "provider_rate_limited"),
        (SpeechUnavailableError("x", "deadline_exceeded"), "deadline_exceeded"),
        (RuntimeError("private-bug"), "internal_error"),
    ],
)
def test_synthesis_failure_after_start_ends_with_one_error_event(client, error, code):
    http, speech = client
    speech.synthesize.side_effect = _stream(["AAA="], error)
    response = http.post(
        "/speech/synthesis", headers=HEADERS,
        json={**SCOPE, "action": "chat_answer", "answer": "검색 결과입니다."},
    )
    assert response.status_code == 200
    assert _events(response) == [{"type": "audio", "delta": "AAA="}, {"type": "error", "code": code}]
    assert "private" not in response.text


def test_synthesis_technical_cap_is_rejected_before_provider_call(client):
    http, speech = client
    body = {**SCOPE, "action": "chat_answer"}
    assert http.post("/speech/synthesis", headers=HEADERS,
                     json={**body, "answer": "가" * (routes.MAX_SYNTHESIS_CHARS + 1)}).status_code == 400
    speech.synthesize.assert_not_called()
    # 예전 4,000자 제품 상한은 없다.
    assert http.post("/speech/synthesis", headers=HEADERS,
                     json={**body, "answer": "가" * 4001}).status_code == 200


def test_route_closes_provider_stream_when_client_stops(client):
    http, speech = client
    closed = []

    def synthesize(text):
        async def gen():
            try:
                yield "AAA="
                yield "BBB="
            finally:
                closed.append(True)

        return gen()

    speech.synthesize.side_effect = synthesize

    async def run():
        response = await routes.synthesize(
            routes.SpeechSynthesisRequest(**SCOPE, action="chat_answer", answer="답변"), speech)
        body = response.body_iterator
        assert json.loads(await body.__anext__())["delta"] == "AAA="
        await body.aclose()

    asyncio.run(run())
    assert closed == [True]


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
    speech.synthesize.assert_not_called()


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
        # 공급사 완료 순서와 달라도 발화 순서로 내보낸다.
        assert [e["segment_id"] for e in finals] == ["item_1", "item_2"]
        assert finals[1]["previous_segment_id"] == "item_1"
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


def test_wav_chunk_records_exact_duration_even_with_token_usage(monkeypatch):
    import io
    import wave

    scope, rows = _ledger_rows(monkeypatch)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 40000)  # 2.5초

    def handler(request):
        return httpx.Response(
            200, json={"text": "전사", "usage": {"type": "tokens", "input_tokens": 9, "output_tokens": 3}}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        openai_speech.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler)),
    )

    async def run():
        with scope:
            await openai_speech.OpenAISpeech("fake-key").transcribe(buffer.getvalue(), "audio/wav")

    asyncio.run(run())
    (transcribe,) = rows.values()
    assert transcribe["audio_seconds"] == 2.5 and transcribe["input_tokens"] == 9


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


async def _collect(text="답변입니다"):
    return [d async for d in openai_speech.OpenAISpeech("fake").synthesize(text)]


def test_tts_streams_pcm_deltas_in_order_with_usage(monkeypatch):
    scope, rows = _ledger_rows(monkeypatch)
    usage = {"input_tokens": 12, "output_tokens": 90,
             "input_token_details": {"cached_tokens": 4}}
    sent, urls = _fake_realtime_tts(
        monkeypatch,
        [_delta(b"\x01\x00" * 10), _delta(b"\x02\x00" * 10),
         {"type": "response.done", "response": {"status": "completed", "usage": usage}}],
    )

    async def run():
        with scope:
            return await _collect()

    deltas = asyncio.run(run())
    assert [base64.b64decode(d) for d in deltas] == [b"\x01\x00" * 10, b"\x02\x00" * 10]
    assert urls == ["wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini"]
    assert [e["type"] for e in sent] == [
        "session.update", "conversation.item.create", "response.create"]
    session = sent[0]["session"]
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["output"] == {
        "format": {"type": "audio/pcm", "rate": 24000}, "voice": "marin"}
    assert sent[1]["item"]["content"] == [{"type": "input_text", "text": "답변입니다"}]
    assert sent[2]["response"]["max_output_tokens"] == "inf"
    (row,) = rows.values()
    assert row["model"] == "gpt-realtime-2.1-mini" and row["status"] == "succeeded"
    assert row["input_characters"] == 5
    assert (row["input_tokens"], row["output_tokens"]) == (12, 90)


@pytest.mark.parametrize(
    "events,code",
    [
        ([{"type": "error", "error": {"message": "private"}}], "provider_unavailable"),
        ([{"type": "error", "error": {"code": "rate_limit_exceeded"}}], "provider_rate_limited"),
        ([{"type": "response.done", "response": {"status": "completed", "usage": {}}}],
         "provider_unavailable"),
        ([_delta(b"\x01\x00" * 100),
          {"type": "response.done",
           "response": {"status": "incomplete", "usage": {"input_tokens": 7, "output_tokens": 3}}}],
         "provider_unavailable"),
    ],
)
def test_tts_error_event_and_empty_audio_are_unavailable(monkeypatch, events, code):
    scope, rows = _ledger_rows(monkeypatch)
    _fake_realtime_tts(monkeypatch, events)

    async def run():
        with scope:
            await _collect("답변")

    with pytest.raises(SpeechUnavailableError, match="음성을 생성") as raised:
        asyncio.run(run())
    assert raised.value.code == code
    (row,) = rows.values()
    assert row["status"] == "failed"
    # 미완료 응답도 공급사가 과금하므로 usage는 원장에 남는다.
    if events[-1].get("response", {}).get("status") == "incomplete":
        assert (row["input_tokens"], row["output_tokens"]) == (7, 3)


def _answer_cancel_with_usage(monkeypatch, usage):
    """response.cancel을 받으면 공급사가 cancelled 응답과 usage를 돌려주는 상황을 만든다."""
    from contextlib import asynccontextmanager

    original = openai_speech.connect

    @asynccontextmanager
    async def connect(url, **kwargs):
        async with original(url, **kwargs) as upstream:
            send = upstream.send

            async def send_and_answer(raw):
                await send(raw)
                if json.loads(raw)["type"] == "response.cancel":
                    await upstream.queue.put(_delta(b"\x03\x00"))
                    await upstream.queue.put(
                        {"type": "response.done", "response": {"status": "cancelled", "usage": usage}})

            upstream.send = send_and_answer
            yield upstream

    monkeypatch.setattr(openai_speech, "connect", connect)


def test_tts_stall_is_deadline_exceeded_and_cancelled_with_usage(monkeypatch):
    scope, rows = _ledger_rows(monkeypatch)
    sent, _ = _fake_realtime_tts(monkeypatch, [_delta(b"\x01\x00")])
    _answer_cancel_with_usage(monkeypatch, {"input_tokens": 12, "output_tokens": 33})
    monkeypatch.setattr(openai_speech, "TTS_EVENT_TIMEOUT_SECONDS", 0.05)

    async def run():
        with scope:
            await _collect()

    with pytest.raises(SpeechUnavailableError) as raised:
        asyncio.run(run())
    assert raised.value.code == "deadline_exceeded"
    assert [e["type"] for e in sent][-1] == "response.cancel"
    (row,) = rows.values()
    assert row["status"] == "cancelled"
    assert (row["input_tokens"], row["output_tokens"]) == (12, 33)


@pytest.mark.parametrize("how", ["aclose", "task_cancel"])
def test_tts_client_stop_cancels_upstream_and_records_usage(monkeypatch, how):
    scope, rows = _ledger_rows(monkeypatch)
    usage = {"input_tokens": 12, "output_tokens": 33}
    sent, _ = _fake_realtime_tts(monkeypatch, [_delta(b"\x01\x00" * 10)])
    _answer_cancel_with_usage(monkeypatch, usage)

    async def run():
        with scope:
            stream = openai_speech.OpenAISpeech("fake").synthesize("답변")
            if how == "aclose":
                await stream.__anext__()
                await stream.aclose()
                return

            async def consume():
                async for _ in stream:
                    await asyncio.sleep(10)  # 소비자가 느려 재생 중 취소되는 상황

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert [e["type"] for e in sent][-1] == "response.cancel"
    (row,) = rows.values()
    assert row["status"] == "cancelled"
    assert (row["input_tokens"], row["output_tokens"]) == (12, 33)
    assert row["input_characters"] == 2


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

    def synthesize(*args):
        async def gen():
            actors.append(usage_ledger._actor.get())
            yield "AAA="

        return gen()

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


class _CompletingUpstream:
    """commit마다 committed·completed를 돌려주는 공급사 가짜. 받은 오디오 길이를 commit 단위로 기록한다."""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.appended = 0
        self.committed_bytes = []

    async def send(self, raw):
        event = json.loads(raw)
        if event["type"] == "session.update":
            await self.queue.put({"type": "session.updated"})
        elif event["type"] == "input_audio_buffer.append":
            self.appended += len(base64.b64decode(event["audio"]))
        elif event["type"] == "input_audio_buffer.commit":
            # 실제 공급사처럼 100ms 미만 commit은 오류로 끝낸다.
            if self.appended < 4800:
                await self.queue.put(
                    {"type": "error", "error": {"code": "input_audio_buffer_commit_empty"}}
                )
                return
            self.committed_bytes.append(self.appended)
            self.appended = 0
            item = f"item_{len(self.committed_bytes)}"
            await self.queue.put({"type": "input_audio_buffer.committed", "item_id": item})
            await self.queue.put(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": item,
                    "transcript": "비밀 전사",
                }
            )

    async def recv(self):
        return json.dumps(await self.queue.get())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()


def _fake_upstream(monkeypatch):
    upstream = _CompletingUpstream()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield upstream

    monkeypatch.setattr(openai_speech, "connect", connect)
    return upstream


def _run_live(packets, scope=None, keep_open=False):
    async def audio():
        for packet in packets:
            yield packet
        if keep_open:
            await asyncio.sleep(1)

    async def run():
        return [
            event
            async for event in openai_speech.OpenAISpeech("fake").transcribe_live(audio())
        ]

    async def run_in_scope():
        with scope:
            return await run()

    return asyncio.run(run_in_scope() if scope else run())


@pytest.mark.parametrize(
    ("tail", "segments", "seconds"),
    [
        # 실서버 재현 경계: 프론트 첫 청크 하나(약 85ms)만 남은 채 멈추면 꼬리를 버리고 끝낸다.
        (4096, 1, [0.1]),
        (4800, 2, [0.1, 0.1]),
        (0, 1, [0.1]),
    ],
)
def test_live_finish_handles_tail_by_provider_minimum(monkeypatch, tail, segments, seconds):
    scope, rows = _ledger_rows(monkeypatch)
    upstream = _fake_upstream(monkeypatch)
    packets = [b"\0" * 4800, "commit"] + ([b"\0" * tail] if tail else []) + ["finish"]

    events = _run_live(packets, scope)

    assert events[-1] == {"type": "finished", "segment_count": segments}
    assert upstream.committed_bytes == [4800] * segments
    # 버린 꼬리는 전사되지 않으므로 사용량에도 넣지 않는다.
    assert [row["audio_seconds"] for row in rows.values()] == seconds
    assert all(row["status"] == "succeeded" for row in rows.values())


def test_live_finish_with_only_short_audio_ends_without_segments(monkeypatch):
    scope, rows = _ledger_rows(monkeypatch)
    upstream = _fake_upstream(monkeypatch)

    events = _run_live([b"\0" * 4096, "finish"], scope)

    assert events[-1] == {"type": "finished", "segment_count": 0}
    assert upstream.committed_bytes == [] and rows == {}


def test_live_short_commit_keeps_audio_for_next_segment(monkeypatch):
    scope, rows = _ledger_rows(monkeypatch)
    upstream = _fake_upstream(monkeypatch)

    events = _run_live(
        [b"\0" * 4096, "commit", b"\0" * 4800, "commit", "finish"], scope
    )

    assert events[-1] == {"type": "finished", "segment_count": 1}
    # 진행 중 짧은 commit은 공급사에 보내지 않고 음성을 다음 구간에 합친다.
    assert upstream.committed_bytes == [8896]
    assert [row["audio_seconds"] for row in rows.values()] == [8896 / 48000]


class _ScriptedUpstream:
    """commit 하나마다 committed와 정해 둔 전사 이벤트를 순서대로 돌려준다."""

    def __init__(self, script):
        self.queue = asyncio.Queue()
        self.script = script
        self.commits = 0

    async def send(self, raw):
        event = json.loads(raw)
        if event["type"] == "session.update":
            await self.queue.put({"type": "session.updated"})
        elif event["type"] == "input_audio_buffer.commit":
            self.commits += 1
            await self.queue.put(
                {
                    "type": "input_audio_buffer.committed",
                    "item_id": f"item_{self.commits}",
                    "previous_item_id": f"item_{self.commits - 1}" if self.commits > 1 else None,
                }
            )
            for kind, item, text in self.script.get(self.commits, ()):
                if kind == "delta":
                    await self.queue.put(
                        {
                            "type": "conversation.item.input_audio_transcription.delta",
                            "item_id": item,
                            "delta": text,
                        }
                    )
                else:
                    await self.queue.put(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": item,
                            "transcript": text,
                        }
                    )

    async def recv(self):
        return json.dumps(await self.queue.get())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()


def _run_scripted(monkeypatch, script, commits):
    upstream = _ScriptedUpstream(script)

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield upstream

    monkeypatch.setattr(openai_speech, "connect", connect)
    packets = [packet for _ in range(commits) for packet in (b"\0" * 4800, "commit")]
    return [e for e in _run_live(packets + ["finish"]) if e["type"] != "ready"]


def _final(item, text):
    return ("completed", item, text)


@pytest.mark.parametrize("empty", ["", "  \n"])
def test_live_segment_without_text_is_not_announced(monkeypatch, empty):
    # 실서버 재현: 정확히 100ms 꼬리와 1초 무음 꼬리는 공급사가 빈 전사로 끝낸다.
    events = _run_scripted(
        monkeypatch, {2: [_final("item_2", empty), _final("item_1", "첫 구간")]}, 2
    )

    assert events == [
        {"type": "committed", "segment_id": "item_1", "previous_segment_id": None},
        {
            "type": "completed",
            "segment_id": "item_1",
            "previous_segment_id": None,
            "text": "첫 구간",
        },
        {"type": "finished", "segment_count": 1},
    ]


def test_live_segment_after_empty_one_links_to_last_announced(monkeypatch):
    events = _run_scripted(
        monkeypatch,
        {
            1: [_final("item_1", "첫 구간")],
            2: [_final("item_2", "")],
            3: [_final("item_3", "셋째 구간")],
        },
        3,
    )

    assert [e for e in events if e["type"] == "committed"] == [
        {"type": "committed", "segment_id": "item_1", "previous_segment_id": None},
        {"type": "committed", "segment_id": "item_3", "previous_segment_id": "item_1"},
    ]
    assert events[-1] == {"type": "finished", "segment_count": 2}


def test_live_all_empty_segments_finish_without_segments(monkeypatch):
    events = _run_scripted(monkeypatch, {1: [_final("item_1", "")]}, 1)

    assert events == [{"type": "finished", "segment_count": 0}]


def test_live_keeps_commit_order_when_later_segment_finishes_first(monkeypatch):
    # Document는 committed 도착 순서로 position을 정한다.
    events = _run_scripted(
        monkeypatch,
        {2: [_final("item_2", "둘째"), _final("item_1", "첫째")]},
        2,
    )

    assert [(e["type"], e.get("segment_id")) for e in events] == [
        ("committed", "item_1"),
        ("completed", "item_1"),
        ("committed", "item_2"),
        ("completed", "item_2"),
        ("finished", None),
    ]


def test_live_delta_with_text_announces_segment_before_completion(monkeypatch):
    events = _run_scripted(
        monkeypatch,
        {1: [("delta", "item_1", " "), ("delta", "item_1", "말"), _final("item_1", "말")]},
        1,
    )

    assert [(e["type"], e.get("text")) for e in events] == [
        ("committed", None),
        ("delta", " "),
        ("delta", "말"),
        ("completed", "말"),
        ("finished", None),
    ]


def test_live_empty_segment_keeps_usage_row(monkeypatch):
    scope, rows = _ledger_rows(monkeypatch)
    upstream = _fake_upstream(monkeypatch)

    original_put = upstream.queue.put

    async def blank(event):
        if "transcript" in event:
            event = {**event, "transcript": ""}
        await original_put(event)

    upstream.queue.put = blank
    events = _run_live([b"\0" * 4800, "finish"], scope)

    assert events[-1] == {"type": "finished", "segment_count": 0}
    # 공급사는 빈 전사에도 과금하므로 원장에는 남긴다.
    assert [row["status"] for row in rows.values()] == ["succeeded"]
    assert [row["audio_seconds"] for row in rows.values()] == [0.1]


def test_live_short_buffer_warning_has_no_audio_or_transcript(monkeypatch, caplog):
    _fake_upstream(monkeypatch)

    with caplog.at_level("WARNING", logger=openai_speech.__name__):
        _run_live([b"\x7f\x7f" * 2048, "commit", "finish"])

    assert "bytes=4096" in caplog.text
    assert "command=commit" in caplog.text and "command=finish" in caplog.text
    assert "비밀 전사" not in caplog.text


def test_live_websocket_short_tail_finishes_normally(client, monkeypatch):
    http, _ = client
    _, rows = _ledger_rows(monkeypatch)
    upstream = _fake_upstream(monkeypatch)
    monkeypatch.setattr(routes, "get_speech", lambda: openai_speech.OpenAISpeech("fake"))

    with http.websocket_connect(
        "/speech/transcriptions/live?workspace_id=w&user_id=u", headers=HEADERS
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"\0" * 4800)
        ws.send_json({"type": "commit"})
        ws.send_bytes(b"\0" * 4096)
        ws.send_json({"type": "finish"})
        received = []
        while not received or received[-1]["type"] not in {"finished", "error"}:
            received.append(ws.receive_json())
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert received[-1] == {"type": "finished", "segment_count": 1}
    assert closed.value.code == 1000
    assert upstream.committed_bytes == [4800]
    assert [row["audio_seconds"] for row in rows.values()] == [0.1]


def test_live_provider_error_logs_code_without_message(monkeypatch, caplog):
    class Failing(_CompletingUpstream):
        async def send(self, raw):
            if json.loads(raw)["type"] == "input_audio_buffer.commit":
                await self.queue.put(
                    {
                        "type": "conversation.item.input_audio_transcription.failed",
                        "error": {"code": "audio_unintelligible", "message": "비밀 전사"},
                    }
                )
            else:
                await super().send(raw)

    upstream = Failing()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield upstream

    monkeypatch.setattr(openai_speech, "connect", connect)

    with caplog.at_level("WARNING", logger=openai_speech.__name__):
        with pytest.raises(SpeechUnavailableError):
            _run_live([b"\0" * 4800, "commit"], keep_open=True)

    assert "audio_unintelligible" in caplog.text
    assert "비밀 전사" not in caplog.text


@pytest.mark.parametrize(
    ("error", "code", "logged", "hidden"),
    [
        (InvalidAudioError("발화는 30초를 넘을 수 없습니다."), "invalid_audio", "발화는 30초", None),
        (RuntimeError("비밀 전사"), "transcription_failed", "RuntimeError", "비밀 전사"),
    ],
)
def test_live_websocket_failure_is_logged_without_content(
    client, monkeypatch, caplog, error, code, logged, hidden
):
    http, _ = client

    class FakeSpeech:
        async def transcribe_live(self, packets):
            yield {"type": "ready"}
            raise error

    monkeypatch.setattr(routes, "get_speech", lambda: FakeSpeech())
    with caplog.at_level("WARNING", logger=routes.__name__):
        with http.websocket_connect(
            "/speech/transcriptions/live?workspace_id=w&user_id=u", headers=HEADERS
        ) as ws:
            assert ws.receive_json()["type"] == "ready"
            assert ws.receive_json()["code"] == code

    assert logged in caplog.text
    assert not hidden or hidden not in caplog.text
