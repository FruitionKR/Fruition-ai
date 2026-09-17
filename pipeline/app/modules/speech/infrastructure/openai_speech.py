import asyncio
import base64
import json
from collections.abc import AsyncIterator

import httpx
from websockets.asyncio.client import connect

from app.modules.speech.domain.errors import InvalidAudioError, SpeechUnavailableError

AUDIO_TYPES = {
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/webm": "webm",
}
MAX_AUDIO_BYTES = 24 * 1024 * 1024


class OpenAISpeech:
    def __init__(self, api_key: str) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def transcribe(self, audio: bytes, media_type: str) -> str:
        if media_type not in AUDIO_TYPES or not 0 < len(audio) <= MAX_AUDIO_BYTES:
            raise InvalidAudioError("지원하는 음성 파일을 24 MiB 이하로 보내주세요.")
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers=self._headers,
                    files={
                        "file": (f"audio.{AUDIO_TYPES[media_type]}", audio, media_type)
                    },
                    data={"model": "gpt-transcribe"},
                )
                response.raise_for_status()
                text = response.json()["text"]
                if not isinstance(text, str):
                    raise TypeError("invalid transcript")
                return text.strip()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise SpeechUnavailableError("음성을 전사하지 못했습니다.") from exc

    async def synthesize(self, text: str) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers=self._headers,
                    json={
                        "model": "gpt-4o-mini-tts",
                        "voice": "coral",
                        "input": text,
                        "response_format": "mp3",
                    },
                )
                response.raise_for_status()
                if not response.content:
                    raise ValueError("empty audio")
                return response.content
        except (httpx.HTTPError, ValueError) as exc:
            raise SpeechUnavailableError("음성을 생성하지 못했습니다.") from exc

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
                                    "transcription": {"model": "gpt-live-transcribe"},
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
