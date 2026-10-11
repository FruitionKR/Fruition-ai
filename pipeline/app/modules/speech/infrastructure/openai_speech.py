import asyncio
import base64
import json
from collections import deque
from collections.abc import AsyncIterator
from time import perf_counter

import anyio
import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from app.modules.model_usage.infrastructure.usage_ledger import finish_call, start_call, track_call_async
from app.modules.speech.domain.errors import InvalidAudioError, SpeechUnavailableError

AUDIO_TYPES = {
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/webm": "webm",
}
MAX_AUDIO_BYTES = 24 * 1024 * 1024
TRANSCRIBE_MODEL = "gpt-transcribe"
# 호출자가 deadline_seconds를 주지 않는 회의 녹음 파일(최대 24 MiB)은 전체 시간 상한 없이 공급사 응답만 이 시간까지 기다린다.
TRANSCRIBE_DEFAULT_TIMEOUT_SECONDS = 900
TTS_MODEL = "gpt-realtime-2.1-mini"
TTS_VOICE = "marin"
TTS_INSTRUCTIONS = (
    "사용자 메시지의 텍스트를 한 글자도 바꾸지 말고 그대로 읽는다. 답하거나 덧붙이지 않는다. "
    "Read the user's message text aloud verbatim. Do not answer, add, or omit anything."
)
LIVE_TRANSCRIBE_MODEL = "gpt-live-transcribe"
TTS_EVENT_TIMEOUT_SECONDS = 60
TTS_CANCEL_WAIT_SECONDS = 5
PCM_BYTES_PER_SECOND = 48000  # PCM16 mono 24 kHz


class OpenAISpeech:
    def __init__(self, api_key: str) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def transcribe(
        self, audio: bytes, media_type: str, deadline_seconds: int | None = None
    ) -> str:
        if media_type not in AUDIO_TYPES or not 0 < len(audio) <= MAX_AUDIO_BYTES:
            raise InvalidAudioError("지원하는 음성 파일을 24 MiB 이하로 보내주세요.")
        try:
            async with (
                # 채팅 음성 전사는 ALB idle timeout(120초) 안에 끝나야 해 호출자가 전체 시간 상한을 준다.
                asyncio.timeout(deadline_seconds),
                track_call_async("openai", TRANSCRIBE_MODEL) as receipt,
                httpx.AsyncClient(
                    timeout=deadline_seconds or TRANSCRIBE_DEFAULT_TIMEOUT_SECONDS
                ) as client,
            ):
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers=self._headers,
                    files={
                        "file": (f"audio.{AUDIO_TYPES[media_type]}", audio, media_type)
                    },
                    data={"model": TRANSCRIBE_MODEL},
                )
                response.raise_for_status()
                body = response.json()
                text = body["text"]
                receipt["usage"] = body.get("usage")
                if not isinstance(text, str):
                    raise TypeError("invalid transcript")
                return text.strip()
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            raise SpeechUnavailableError("음성을 전사하지 못했습니다.") from exc

    async def synthesize(self, text: str) -> AsyncIterator[str]:
        """원문 낭독 PCM16 24 kHz 오디오를 base64 delta 그대로 흘려보낸다.

        소비자가 중단하면(CancelledError·aclose) 공급사에 response.cancel을 보내고
        response.done의 usage를 원장에 남긴다. 취소된 응답도 생성된 만큼 과금된다.
        """
        # gpt-4o-mini-tts는 종료 예정이라 Realtime 모델로 원문을 낭독시킨다.
        call_id = await asyncio.to_thread(start_call, "openai", TTS_MODEL)
        receipt = {"input_characters": len(text)}
        started = perf_counter()
        # 중단·타임아웃으로 취소한 응답은 cancelled로 남기고 생성된 만큼의 usage를 채운다.
        status = "failed"
        try:
            async with connect(
                f"wss://api.openai.com/v1/realtime?model={TTS_MODEL}",
                additional_headers=self._headers,
                max_size=4 * 1024 * 1024,
                open_timeout=10,
                close_timeout=5,
            ) as upstream:
                # Realtime에는 temperature 설정이 없어 지침으로 원문 낭독을 강제한다.
                await upstream.send(
                    json.dumps(
                        {
                            "type": "session.update",
                            "session": {
                                "type": "realtime",
                                "output_modalities": ["audio"],
                                "instructions": TTS_INSTRUCTIONS,
                                "audio": {
                                    "output": {
                                        "format": {"type": "audio/pcm", "rate": 24000},
                                        "voice": TTS_VOICE,
                                    }
                                },
                            },
                        }
                    )
                )
                async with asyncio.timeout(15):
                    while True:
                        event = json.loads(await upstream.recv())
                        if event["type"] == "error":
                            raise _provider_error(event)
                        if event["type"] == "session.updated":
                            break
                await upstream.send(
                    json.dumps(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": text}],
                            },
                        }
                    )
                )
                # 긴 답변이 중간에 잘리지 않도록 출력 토큰 상한을 두지 않는다.
                await upstream.send(
                    json.dumps(
                        {
                            "type": "response.create",
                            "response": {"max_output_tokens": "inf"},
                        }
                    )
                )
                produced = False
                try:
                    while True:
                        # 전체 시간이 아니라 이벤트 간 간격만 제한한다. 긴 낭독도 오디오가 계속 오면 끊지 않는다.
                        async with asyncio.timeout(TTS_EVENT_TIMEOUT_SECONDS):
                            event = json.loads(await upstream.recv())
                        if event["type"] == "error":
                            raise _provider_error(event)
                        if event["type"] == "response.output_audio.delta":
                            produced = True
                            yield event["delta"]
                        elif event["type"] == "response.done":
                            receipt["usage"] = event["response"].get("usage")
                            # 중간에 끊긴 응답의 일부 오디오를 성공으로 돌려주지 않는다.
                            if event["response"].get("status") != "completed":
                                raise ValueError("incomplete response")
                            break
                except (asyncio.CancelledError, GeneratorExit, TimeoutError):
                    status = "cancelled"
                    await _cancel_response(upstream, receipt)
                    raise
                if not produced:
                    raise ValueError("empty audio")
            status = "succeeded"
        except InvalidStatus as exc:
            code = "provider_rate_limited" if exc.response.status_code == 429 else "provider_unavailable"
            raise SpeechUnavailableError("음성을 생성하지 못했습니다.", code) from exc
        except TimeoutError as exc:
            raise SpeechUnavailableError("음성을 생성하지 못했습니다.", "deadline_exceeded") from exc
        except (OSError, WebSocketException, ValueError, KeyError, TypeError) as exc:
            raise SpeechUnavailableError("음성을 생성하지 못했습니다.") from exc
        finally:
            # 취소 중에도 원장 쓰기가 중단되지 않게 보호한다.
            with anyio.CancelScope(shield=True):
                await asyncio.to_thread(
                    finish_call, call_id, status, receipt, TTS_MODEL,
                    (perf_counter() - started) * 1000,
                )

    async def transcribe_live(
        self, audio: AsyncIterator[bytes | str]
    ) -> AsyncIterator[dict]:
        # 원본 오디오는 저장하지 않는다. 확정 전사 저장은 호출 서비스의 책임이다.
        async with connect(
            "wss://api.openai.com/v1/realtime?intent=transcription",
            additional_headers=self._headers,
            max_size=1024 * 1024,
            max_queue=8,
            open_timeout=10,
            close_timeout=5,
        ) as upstream:
            await upstream.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "transcription",
                            "audio": {
                                "input": {
                                    "format": {"type": "audio/pcm", "rate": 24000},
                                    "transcription": {"model": LIVE_TRANSCRIBE_MODEL},
                                    "turn_detection": None,
                                }
                            },
                        },
                    }
                )
            )
            # 모델 설정이 승인되기 전에는 오디오를 받지 않는다.
            async with asyncio.timeout(15):
                while True:
                    event = json.loads(await upstream.recv())
                    if event["type"] == "error":
                        raise SpeechUnavailableError(
                            "실시간 전사를 시작하지 못했습니다."
                        )
                    if event["type"] == "session.updated":
                        break
            yield {"type": "ready", "sample_rate": 24000}
            queue: asyncio.Queue[dict | Exception] = asyncio.Queue(maxsize=32)
            sent = 0
            completed: set[str] = set()
            committed: dict[str, str | None] = {}
            # 확정 구간마다 원장 한 행을 남겨 document가 세션 중에도 사용량을 볼 수 있게 한다.
            commit_bytes: deque[int] = deque()
            segment_calls: dict[str, tuple[str | None, float | None, float]] = {}
            finishing = False

            async def send_audio() -> None:
                nonlocal sent, finishing
                buffered = 0
                total = 0
                try:
                    async for packet in audio:
                        if isinstance(packet, bytes):
                            if not packet or len(packet) % 2 or len(packet) > 48000:
                                raise InvalidAudioError(
                                    "PCM16 mono 24 kHz, 최대 1초 프레임이 필요합니다."
                                )
                            buffered += len(packet)
                            total += len(packet)
                            if buffered > 48000 * 30 or total > 48000 * 3600:
                                raise InvalidAudioError(
                                    "발화는 30초, 세션은 60분을 넘을 수 없습니다."
                                )
                            await upstream.send(
                                json.dumps(
                                    {
                                        "type": "input_audio_buffer.append",
                                        "audio": base64.b64encode(packet).decode(),
                                    }
                                )
                            )
                        elif packet in {"commit", "finish"}:
                            if buffered:
                                if buffered < 4800:
                                    raise InvalidAudioError(
                                        "발화는 최소 100ms가 필요합니다."
                                    )
                                sent += 1
                                if sent - len(completed) > 8:
                                    raise InvalidAudioError(
                                        "전사 처리 대기 중입니다. 전송 속도를 줄여주세요."
                                    )
                                commit_bytes.append(buffered)
                                await upstream.send(
                                    json.dumps({"type": "input_audio_buffer.commit"})
                                )
                                buffered = 0
                            if packet == "finish":
                                finishing = True
                                await queue.put({"type": "finish_requested"})
                                return
                        else:
                            raise InvalidAudioError(
                                "commit 또는 finish 명령이 필요합니다."
                            )
                    raise InvalidAudioError("finish 없이 음성 입력이 종료됐습니다.")
                except Exception as exc:  # noqa: BLE001 - 생산자 오류를 소비자에 전달하고 양쪽 작업을 정리한다.
                    await queue.put(exc)

            async def receive_events() -> None:
                try:
                    async for raw in upstream:
                        await queue.put(json.loads(raw))
                    await queue.put(SpeechUnavailableError("전사 연결이 종료됐습니다."))
                except Exception as exc:  # noqa: BLE001 - 수신 작업의 오류가 호출자에게 전달되도록 한다.
                    await queue.put(exc)

            tasks = [
                asyncio.create_task(send_audio()),
                asyncio.create_task(receive_events()),
            ]
            try:
                async with asyncio.timeout(3600):
                    while True:
                        event = await asyncio.wait_for(queue.get(), timeout=60)
                        if isinstance(event, Exception):
                            raise event
                        kind = event.get("type")
                        if kind in {
                            "error",
                            "conversation.item.input_audio_transcription.failed",
                        }:
                            raise SpeechUnavailableError("실시간 전사가 실패했습니다.")
                        if kind == "input_audio_buffer.committed":
                            committed[event["item_id"]] = event.get("previous_item_id")
                            seconds = (
                                commit_bytes.popleft() / PCM_BYTES_PER_SECOND
                                if commit_bytes
                                else None
                            )
                            call_id = await asyncio.to_thread(
                                start_call, "openai", LIVE_TRANSCRIBE_MODEL
                            )
                            segment_calls[event["item_id"]] = (
                                call_id,
                                seconds,
                                perf_counter(),
                            )
                            yield {
                                "type": "committed",
                                "segment_id": event["item_id"],
                                "previous_segment_id": event.get("previous_item_id"),
                            }
                        elif (
                            kind == "conversation.item.input_audio_transcription.delta"
                        ):
                            yield {
                                "type": "delta",
                                "segment_id": event["item_id"],
                                "text": event["delta"],
                            }
                        elif (
                            kind
                            == "conversation.item.input_audio_transcription.completed"
                        ):
                            item_id = event["item_id"]
                            if item_id not in committed:
                                raise SpeechUnavailableError(
                                    "전사 구간 순서를 확인하지 못했습니다."
                                )
                            if item_id not in completed:
                                completed.add(item_id)
                                await _finish_segment(
                                    segment_calls.pop(item_id, None),
                                    "succeeded",
                                    event.get("usage"),
                                )
                                yield {
                                    "type": "completed",
                                    "segment_id": item_id,
                                    "previous_segment_id": committed[item_id],
                                    "text": event["transcript"],
                                }
                        if finishing and len(completed) == sent:
                            yield {"type": "finished", "segment_count": len(completed)}
                            return
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                # 전사를 받지 못한 구간도 오디오는 공급사에 보냈으므로 failed로 남긴다.
                for call in segment_calls.values():
                    await _finish_segment(call, "failed", None)


async def _finish_segment(
    call: tuple[str | None, float | None, float] | None, status: str, usage
) -> None:
    if call is None:
        return
    call_id, seconds, started = call
    await asyncio.to_thread(
        finish_call,
        call_id,
        status,
        {"usage": usage, "audio_seconds": seconds},
        LIVE_TRANSCRIBE_MODEL,
        (perf_counter() - started) * 1000,
    )


def _provider_error(event: dict) -> SpeechUnavailableError:
    code = (event.get("error") or {}).get("code")
    return SpeechUnavailableError(
        "음성을 생성하지 못했습니다.",
        "provider_rate_limited" if code == "rate_limit_exceeded" else "provider_unavailable",
    )


async def _cancel_response(upstream, receipt: dict) -> None:
    """클라이언트 중단 시 생성을 멈추고 response.done의 usage를 받는다. 최대 5초 기다린다."""
    with anyio.CancelScope(shield=True), anyio.move_on_after(TTS_CANCEL_WAIT_SECONDS):
        try:
            await upstream.send(json.dumps({"type": "response.cancel"}))
            while True:
                event = json.loads(await upstream.recv())
                if event["type"] == "response.done":
                    receipt["usage"] = event["response"].get("usage")
                    return
                if event["type"] == "error":
                    return
        except (OSError, WebSocketException, ValueError, KeyError, TypeError):
            return
