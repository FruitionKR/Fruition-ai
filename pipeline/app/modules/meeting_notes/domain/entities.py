from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptSegment:
    id: str
    text: str


class InvalidMeetingNotesError(ValueError):
    """회의록 결과의 필수 정보 또는 전사 근거가 잘못됨."""
