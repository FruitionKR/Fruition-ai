import asyncio
import tempfile
from collections.abc import Callable
from math import ceil
from pathlib import Path
from typing import Any, Protocol

from app.core.pipeline_control import DeadlineExceededError, ensure_task_active, remaining_seconds, request_deadline
from app.modules.speech.application.ports import SpeechPort
from app.modules.speech.domain.errors import MeetingTranscriptionError

MAX_TRANSCRIPT_CHARS = 100_000


class MeetingStoragePort(Protocol):
    def download(self, key: str, path: Path) -> None: ...
    def load_checkpoints(self, workspace_id: str, run_id: str) -> dict[int, str]: ...
    def save_checkpoint(self, workspace_id: str, run_id: str, index: int, text: str) -> None: ...


class TranscribeMeetingRecording:
    """녹음을 내려받아 구간으로 나누고, 체크포인트가 없는 구간만 전사해 순서대로 합친다."""

    def __init__(
        self,
        speech: SpeechPort,
        storage: MeetingStoragePort,
        split: Callable[[Path, Path], tuple[list[Path], float]],
        concurrency: int,
        deadline_seconds: float,
    ) -> None:
        self._speech = speech
        self._storage = storage
        self._split = split
        self._concurrency = concurrency
        self._deadline_seconds = deadline_seconds

    async def execute(self, workspace_id: str, run_id: str, meeting_id: str, recording_key: str) -> dict[str, Any]:
        # ponytail: 디코딩·다운로드는 스레드라 마감에 걸려도 끝날 때까지 돈다. 길어지면 프로세스 분리를 검토한다.
        with request_deadline(self._deadline_seconds):
            timeout = asyncio.timeout(self._deadline_seconds)
            try:
                async with timeout:
                    return await self._run(workspace_id, run_id, meeting_id, recording_key)
            except TimeoutError as exc:
                if not timeout.expired():
                    raise
                raise DeadlineExceededError("Request deadline exceeded.") from exc

    async def _run(self, workspace_id: str, run_id: str, meeting_id: str, recording_key: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="meeting-transcription-") as tmp:
            work = Path(tmp)
            await asyncio.to_thread(self._storage.download, recording_key, work / "recording")
            await asyncio.to_thread(ensure_task_active)
            paths, duration = await asyncio.to_thread(self._split, work / "recording", work / "chunks")
            done = await asyncio.to_thread(self._storage.load_checkpoints, workspace_id, run_id)
            slots = asyncio.Semaphore(self._concurrency)

            async def transcribe(index: int, path: Path) -> str:
                if index in done:
                    return done[index]
                async with slots:
                    # 대기하던 구간은 취소·마감을 확인한 뒤에만 공급사를 부른다.
                    await asyncio.to_thread(ensure_task_active)
                    audio = await asyncio.to_thread(path.read_bytes)
                    text = await self._speech.transcribe(audio, "audio/wav", max(1, ceil(remaining_seconds() or 1)))
                    await asyncio.to_thread(self._storage.save_checkpoint, workspace_id, run_id, index, text)
                    return text

            tasks = [asyncio.create_task(transcribe(i, p)) for i, p in enumerate(paths)]
            try:
                texts = await asyncio.gather(*tasks)
            except BaseException:
                # 한 구간이 실패·취소되면 남은 구간은 공급사를 더 부르지 않는다.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        text = " ".join(t for t in texts if t)
        if not text:
            raise MeetingTranscriptionError("MEETING_TRANSCRIPT_EMPTY")
        if len(text) > MAX_TRANSCRIPT_CHARS:
            raise MeetingTranscriptionError("MEETING_TRANSCRIPT_TOO_LONG")
        return {"meeting_id": meeting_id, "text": text, "duration_seconds": round(duration, 1), "chunks": len(paths)}
