from collections.abc import AsyncIterator
from typing import Protocol


class SpeechPort(Protocol):
    async def transcribe(
        self, audio: bytes, media_type: str, deadline_seconds: int | None = None
    ) -> str: ...

    def synthesize(self, text: str) -> AsyncIterator[str]: ...

    def transcribe_live(
        self, audio: AsyncIterator[bytes | str]
    ) -> AsyncIterator[dict]: ...
