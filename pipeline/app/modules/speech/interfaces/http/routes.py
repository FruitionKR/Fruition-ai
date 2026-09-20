import asyncio
import json
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
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.modules.speech.application.ports import SpeechPort
from app.modules.speech.domain.errors import InvalidAudioError, SpeechUnavailableError
from app.modules.speech.interfaces.http.dependencies import authorize_speech, get_speech
from app.modules.speech.interfaces.http.schemas import (
    SpeechSynthesisRequest,
    TranscriptionResponse,
)

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
    workspace_id: str = Query(min_length=1, max_length=128),
    user_id: str = Query(min_length=1, max_length=128),
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
    try:
        return TranscriptionResponse(
            text=await speech.transcribe(bytes(audio), media_type)
        )
    except InvalidAudioError as exc:
        raise HTTPException(422, str(exc)) from exc
    except SpeechUnavailableError as exc:
        raise HTTPException(502, "음성을 전사하지 못했습니다.") from exc


@router.post(
    "/synthesis",
    response_class=Response,
    responses={200: {"content": {"audio/mpeg": {}}}},
)
async def synthesize(
    payload: SpeechSynthesisRequest, speech: Annotated[SpeechPort, Depends(get_speech)]
) -> Response:
    await run_in_threadpool(authorize_speech, payload.workspace_id, payload.user_id)
    try:
        audio = await speech.synthesize(payload.answer)
    except SpeechUnavailableError as exc:
        raise HTTPException(502, "음성을 생성하지 못했습니다.") from exc
    return Response(
        audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"}
    )


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

    try:
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
