import os

from app.core.llm_env import api_key_from_env
from app.modules.speech.application.transcribe_meeting import TranscribeMeetingRecording
from app.modules.speech.domain.errors import SpeechUnavailableError
from app.modules.speech.infrastructure.audio_chunks import split_recording
from app.modules.speech.infrastructure.meeting_storage import S3MeetingStorage
from app.modules.speech.infrastructure.openai_speech import OpenAISpeech


def build_meeting_transcription() -> TranscribeMeetingRecording:
    key = api_key_from_env(provider="openai", strip=True)
    if not key:
        raise SpeechUnavailableError("음성 제공자가 설정되지 않았습니다.")
    return TranscribeMeetingRecording(
        OpenAISpeech(key),
        S3MeetingStorage(),
        split_recording,
        # 마감은 maintenance worker의 max_poll_interval(1800초)보다 짧아야 한다.
        concurrency=int(os.environ.get("MEETING_TRANSCRIPTION_CONCURRENCY", "4")),
        deadline_seconds=float(os.environ.get("MEETING_TRANSCRIPTION_DEADLINE_SECONDS", "1500")),
    )
