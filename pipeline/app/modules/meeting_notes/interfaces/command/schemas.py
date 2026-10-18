from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.llm_env import resolve_workspace_llm_selection
from app.modules.speech.interfaces.http.schemas import SpeechScope


class MeetingSegment(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=10000)


class MeetingNotesCommand(SpeechScope):
    """`ai.maintenance.command`의 회의록 생성 command. document가 run을 정하고 보낸다."""

    run_id: str = Field(min_length=1, max_length=128)
    kind: Literal["meeting_notes"]
    meeting_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    display_name: str = Field(default="회의록", min_length=1, max_length=200)
    segments: list[MeetingSegment] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_transcript(self):
        # 워크스페이스 공용 모델 설정에서 온 값이다. 허용 범위 밖이면 워크스페이스 기본 모델로 대체한다.
        self.provider, self.model = resolve_workspace_llm_selection(self.provider, self.model)
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
