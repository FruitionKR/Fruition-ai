from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.speech.interfaces.http.schemas import SpeechScope


class MeetingSegment(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=10000)


class MeetingNotesRequest(SpeechScope):
    display_name: str = Field(default="회의록", min_length=1, max_length=200)
    segments: list[MeetingSegment] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_transcript(self):
        if sum(len(segment.text) for segment in self.segments) > 100000:
            raise ValueError("전사는 100,000자 이하로 보내주세요.")
        if len({segment.id for segment in self.segments}) != len(self.segments):
            raise ValueError("전사 구간 ID가 중복됐습니다.")
        return self


class MeetingNoteItem(BaseModel):
    text: str
    source_segment_ids: list[str]


class MeetingNotesResponse(BaseModel):
    display_name: str
    markdown: str
    summary: list[MeetingNoteItem]
    decisions: list[MeetingNoteItem]
    action_items: list[MeetingNoteItem]
    open_questions: list[MeetingNoteItem]
