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
    # 회의록 머리의 날짜·참석자. document가 알고 있으면 보낸다. 없으면 날짜는 "확인되지 않음",
    # 참석자는 전사에서 이름이 확인된 사람만 쓴다.
    meeting_date: str | None = Field(default=None, min_length=1, max_length=40)
    attendees: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_attendees(self):
        if any(not name.strip() or len(name) > 100 for name in self.attendees):
            raise ValueError("참석자 이름은 1~100자로 보내주세요.")
        return self

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


class MeetingActionItem(MeetingNoteItem):
    # 발언으로 확인된 경우에만 채운다.
    owner: str | None = None
    due: str | None = None


class MeetingDiscussionTopic(BaseModel):
    topic: str
    items: list[MeetingNoteItem]


class MeetingNotesResponse(BaseModel):
    display_name: str
    # Figma 시안 구성의 본문: 날짜·참석자, 회의 요약, 주요 논의(주제별), 결정 사항, 해야 할 일, 다음 일정.
    markdown: str
    meeting_date: str | None = None
    attendees: list[str] = []
    summary: list[MeetingNoteItem]
    discussion: list[MeetingDiscussionTopic] = []
    decisions: list[MeetingNoteItem]
    action_items: list[MeetingActionItem]
    next_schedule: list[MeetingNoteItem] = []
    # 본문에는 싣지 않지만 기존 소비자를 위해 배열은 유지한다.
    open_questions: list[MeetingNoteItem]
