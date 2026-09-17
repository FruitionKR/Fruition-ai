from fastapi import HTTPException

from app.core.llm_env import api_key_from_env
from app.modules.meeting_notes.application.generate_meeting_notes import (
    GenerateMeetingNotes,
)
from app.modules.meeting_notes.infrastructure.chat_meeting_notes import ChatMeetingNotes
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)


def get_meeting_notes() -> GenerateMeetingNotes:
    key = api_key_from_env(provider="openai", strip=True)
    if not key:
        raise HTTPException(503, "회의록 생성 모델이 설정되지 않았습니다.")
    return GenerateMeetingNotes(
        ChatMeetingNotes(
            ChatCompletionsJsonClient(
                ChatClientConfig(
                    api_key=key, provider="openai", model="gpt-5-nano", json_mode=True
                ),
            )
        )
    )
