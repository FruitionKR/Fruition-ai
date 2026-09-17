from fastapi import HTTPException

from app.core.llm_env import api_key_from_env
from app.modules.skill.infrastructure.workspace_authorization import get_workspace_role
from app.modules.speech.application.ports import SpeechPort
from app.modules.speech.infrastructure.openai_speech import OpenAISpeech


def authorize_speech(workspace_id: str, user_id: str) -> None:
    try:
        role = get_workspace_role(workspace_id, user_id)
    except RuntimeError as exc:
        raise HTTPException(503, "음성 기능 권한을 확인하지 못했습니다.") from exc
    if role not in {"OWNER", "MEMBER"}:
        raise HTTPException(403, "워크스페이스 접근 권한이 없습니다.")


def get_speech() -> SpeechPort:
    key = api_key_from_env(provider="openai", strip=True)
    if not key:
        raise HTTPException(503, "음성 제공자가 설정되지 않았습니다.")
    return OpenAISpeech(key)
