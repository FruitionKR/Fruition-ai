import re
from typing import Literal

from pydantic import Field, model_validator

from app.modules.speech.interfaces.http.schemas import SpeechScope

RECORDING_KEY = re.compile(r"^meetings/([A-Za-z0-9_-]{1,128})/recording-[0-9a-f-]{36}\.(wav|mp3|m4a|webm)$")


class MeetingTranscriptionCommand(SpeechScope):
    """`ai.maintenance.command`의 회의 녹음 전사 command. document가 run을 정하고 보낸다."""

    run_id: str = Field(min_length=1, max_length=128)
    kind: Literal["meeting_transcription"]
    meeting_id: str = Field(min_length=1, max_length=128)
    recording_key: str = Field(max_length=300)
    content_type: Literal["audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/webm"]

    @model_validator(mode="after")
    def validate_recording_key(self):
        match = RECORDING_KEY.fullmatch(self.recording_key)
        if match is None or match.group(1) != self.meeting_id:
            raise ValueError("녹음 키가 회의와 맞지 않습니다.")
        return self
