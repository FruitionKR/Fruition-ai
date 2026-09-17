from typing import Protocol

from app.modules.meeting_notes.domain.entities import TranscriptSegment


class MeetingNotesGeneratorPort(Protocol):
    def generate(
        self, display_name: str, segments: list[TranscriptSegment]
    ) -> dict: ...
