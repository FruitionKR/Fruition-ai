class SpeechUnavailableError(RuntimeError):
    """음성 제공자 설정 누락 또는 호출 실패. code는 스트림 error 이벤트의 공통 코드다."""

    def __init__(self, message: str = "", code: str = "provider_unavailable") -> None:
        super().__init__(message)
        self.code = code


class InvalidAudioError(ValueError):
    """음성 입력 또는 스트림 순서가 계약과 다름."""


class MeetingTranscriptionError(RuntimeError):
    """회의 녹음 전사 실패. code는 결과 이벤트 error에 그대로 실린다(예외 원문은 싣지 않는다)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
