from fastapi import HTTPException

from app.core.llm_env import api_key_from_env, provider_api_key_env
from app.modules.meeting_notes.application.generate_meeting_notes import (
    GenerateMeetingNotes,
)
from app.modules.meeting_notes.infrastructure.chat_meeting_notes import (
    BATCH_TIMEOUT_SECONDS,
    MAX_BATCH_RETRIES,
    MAX_OUTPUT_TOKENS,
    ChatMeetingNotes,
)
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)


def build_meeting_notes(provider: str, model: str) -> GenerateMeetingNotes:
    key = api_key_from_env(provider=provider, strip=True)
    if not key:
        raise HTTPException(
            503, f"회의록 생성 모델이 설정되지 않았습니다. {provider_api_key_env(provider)}를 확인해주세요."
        )
    return GenerateMeetingNotes(
        ChatMeetingNotes(
            ChatCompletionsJsonClient(
                ChatClientConfig(
                    api_key=key,
                    provider=provider,
                    model=model,
                    json_mode=True,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    timeout_seconds=BATCH_TIMEOUT_SECONDS,
                    max_retries=MAX_BATCH_RETRIES,
                ),
            )
        )
    )
