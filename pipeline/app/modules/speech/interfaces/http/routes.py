import asyncio
import json
import logging
import os
from contextlib import aclosing
from hmac import compare_digest
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.modules.model_usage.infrastructure.usage_ledger import usage_scope
from app.modules.model_usage.interfaces.http.dependencies import RequestId
from app.modules.speech.application.ports import SpeechPort
from app.modules.speech.domain.errors import InvalidAudioError, SpeechUnavailableError
from app.modules.speech.interfaces.http.dependencies import authorize_speech, get_speech
from app.modules.speech.interfaces.http.schemas import (
    SpeechSynthesisRequest,
    TranscriptionResponse,
)

logger = logging.getLogger(__name__)
# 모델 입력 한도 기반 기술 상한이며 제품 상한이 아니다.
# gpt-realtime-2.1-mini는 응답 하나에 출력 32,000 토큰까지 낸다(공식 모델 문서). 음성은 분당 약 1,200 토큰이라
# 한 번에 약 26분, 한국어로 대략 8,000~9,000자를 읽는다. 그보다 긴 답변은 끝까지 읽지 못하므로 시작 전에 거절한다.
# ponytail: 분당 토큰 수는 외부 자료의 추정치다. 실제 사용량 원장 값으로 다시 맞춘다.
MAX_SYNTHESIS_CHARS = 8000

router = APIRouter(prefix="/speech", tags=["speech"])


@router.post(
    "/transcriptions",
    response_model=TranscriptionResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                media: {"schema": {"type": "string", "format": "binary"}}
                for media in ("audio/wav", "audio/mpeg", "audio/mp4", "audio/webm")
            },
        },
    },
)
async def transcribe(
    request: Request,
    speech: Annotated[SpeechPort, Depends(get_speech)],
    request_id: RequestId = None,
    workspace_id: str = Query(min_length=1, max_length=128),
    user_id: str = Query(min_length=1, max_length=128),
    # 채팅처럼 ALB idle timeout 안에 끝나야 하는 호출자만 보낸다. 없으면 전체 시간 상한이 없다.
    deadline_seconds: int | None = Query(default=None, ge=1, le=600),
) -> TranscriptionResponse:
    await run_in_threadpool(authorize_speech, workspace_id, user_id)
    media_type = request.headers.get("content-type", "").split(";")[0].strip()
    if media_type not in {"audio/wav", "audio/mpeg", "audio/mp4", "audio/webm"}:
        raise HTTPException(415, "WAV, MP3, M4A, WebM 음성을 보내주세요.")
    audio = bytearray()
    async for chunk in request.stream():
        if len(audio) + len(chunk) > 24 * 1024 * 1024:
            raise HTTPException(413, "음성 파일은 24 MiB 이하로 보내주세요.")
        audio.extend(chunk)
    if not audio:
        raise HTTPException(422, "음성 파일이 비어 있습니다.")
    scope = {"run_id": request_id, "workspace_id": workspace_id, "user_id": user_id,
             "kind": "speech_transcription"}
    try:
        with usage_scope(scope):
            text = await speech.transcribe(bytes(audio), media_type, deadline_seconds)
        return TranscriptionResponse(text=text)
    except InvalidAudioError as exc:
        raise HTTPException(422, str(exc)) from exc
    except SpeechUnavailableError as exc:
        raise HTTPException(502, "음성을 전사하지 못했습니다.") from exc


@router.post(
    "/synthesis",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"application/x-ndjson": {}}},
        400: {"description": "답변이 기술 상한(MAX_SYNTHESIS_CHARS)을 넘는다."},
    },
)
async def synthesize(
    payload: SpeechSynthesisRequest,
    speech: Annotated[SpeechPort, Depends(get_speech)],
    request_id: RequestId = None,
) -> StreamingResponse:
    await run_in_threadpool(authorize_speech, payload.workspace_id, payload.user_id)
    if len(payload.answer) > MAX_SYNTHESIS_CHARS:
        raise HTTPException(400, f"답변은 {MAX_SYNTHESIS_CHARS:,}자 이하로 보내주세요.")
    scope = {"run_id": request_id, "workspace_id": payload.workspace_id,
             "user_id": payload.user_id, "kind": "speech_synthesis"}

    async def events():
        # 클라이언트가 끊으면 Starlette가 이 제너레이터를 취소·종료하고, aclosing이 공급사 쪽 취소까지 전파한다.
        with usage_scope(scope):
            try:
                async with aclosing(speech.synthesize(payload.answer)) as deltas:
                    async for delta in deltas:
                        yield _line({"type": "audio", "delta": delta})
                yield _line({"type": "done"})
            except SpeechUnavailableError as exc:
                yield _line({"type": "error", "code": exc.code})
            except Exception:  # noqa: BLE001 - 시작된 스트림은 상태 코드를 바꿀 수 없어 error 이벤트로 닫는다.
                logger.exception("음성 합성 스트림 실패")
                yield _line({"type": "error", "code": "internal_error"})

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store"},
    )


def _line(event: dict) -> bytes:
    return json.dumps(event, separators=(",", ":")).encode() + b"\n"


@router.websocket("/transcriptions/live")
async def transcribe_live(websocket: WebSocket) -> None:
    # HTTP middleware는 WebSocket에 적용되지 않으므로 accept 전에 직접 검증한다.
    expected = os.environ.get("INTERNAL_CALLBACK_TOKEN", "")
    token = websocket.headers.get("X-Internal-Token", "")
    if not expected or not compare_digest(token, expected):
        await websocket.close(code=1008)
        return
    workspace_id = websocket.query_params.get("workspace_id", "").strip()
    user_id = websocket.query_params.get("user_id", "").strip()
    if not workspace_id or not user_id or max(len(workspace_id), len(user_id)) > 128:
        await websocket.close(code=1008)
        return
    try:
        await run_in_threadpool(authorize_speech, workspace_id, user_id)
        speech = get_speech()
    except HTTPException:
        await websocket.close(code=1008)
        return
    await websocket.accept()

    async def packets():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("bytes") is not None:
                yield message["bytes"]
            else:
                text = message.get("text", "")
                if len(text) > 100:
                    raise InvalidAudioError("명령이 너무 큽니다.")
                try:
                    command = json.loads(text)
                except ValueError as exc:
                    raise InvalidAudioError("JSON 명령이 필요합니다.") from exc
                if not isinstance(command, dict) or command.get("type") not in {
                    "commit",
                    "finish",
                }:
                    raise InvalidAudioError("commit 또는 finish 명령이 필요합니다.")
                yield command["type"]
                if command["type"] == "finish":
                    return

    scope = {"run_id": websocket.headers.get("X-Request-Id", "")[:128], "workspace_id": workspace_id,
             "user_id": user_id, "kind": "speech_live_transcription"}
    try:
        with usage_scope(scope):
            async with (
                asyncio.timeout(3600),
                aclosing(speech.transcribe_live(packets())) as events,
            ):
                async for event in events:
                    await websocket.send_json(event)
        await websocket.close(code=1000)
    except WebSocketDisconnect:
        return
    except InvalidAudioError as exc:
        await websocket.send_json(
            {"type": "error", "code": "invalid_audio", "message": str(exc)}
        )
        await websocket.close(code=1008)
    except Exception:  # noqa: BLE001 - 외부 제공자 오류와 연결 종료의 세부 정보는 클라이언트에 노출하지 않는다.
        await websocket.send_json(
            {
                "type": "error",
                "code": "transcription_failed",
                "message": "전사가 중단됐습니다. 확정된 구간을 보관하고 다시 연결해주세요.",
            }
        )
        await websocket.close(code=1011)
