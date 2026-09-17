class SpeechUnavailableError(RuntimeError):
    """음성 제공자 설정 누락 또는 호출 실패."""


class InvalidAudioError(ValueError):
    """음성 입력 또는 스트림 순서가 계약과 다름."""
