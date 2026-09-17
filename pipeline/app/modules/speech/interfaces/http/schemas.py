from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SpeechScope(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)


class SpeechSynthesisRequest(SpeechScope):
    action: Literal["chat_answer"]
    answer: str = Field(min_length=1, max_length=4000)


class TranscriptionResponse(BaseModel):
    text: str
