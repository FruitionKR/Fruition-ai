from collections.abc import AsyncIterator
from typing import Protocol


class SpeechPort(Protocol):
    async def transcribe(self, audio: bytes, media_type: str) -> str: ...

    async def synthesize(self, text: str) -> bytes: ...

    def transcribe_live(
        self, audio: AsyncIterator[bytes | str]
    ) -> AsyncIterator[dict]: ...
