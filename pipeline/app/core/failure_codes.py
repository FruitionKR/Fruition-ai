from __future__ import annotations

import json
import re

import httpx

from app.core.pipeline_control import PipelineRunCancelledError

# 사용자에게 보이는 실패 사유는 코드만 내보낸다. 문구는 document가 코드별로 한 곳에서 만든다.
# 예외 원문은 ai 로그에만 남긴다.
PROVIDER_RATE_LIMITED = "provider_rate_limited"
PROVIDER_UNAVAILABLE = "provider_unavailable"
INPUT_REJECTED = "input_rejected"
DEADLINE_EXCEEDED = "deadline_exceeded"
CANCELLED = "cancelled"
INTERNAL_ERROR = "internal_error"

_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
# openai·anthropic·google-genai는 연결 오류와 시간 초과에 status가 없어 클래스 이름으로 가른다.
_UNAVAILABLE_NAMES = {"APIConnectionError", "APITimeoutError", "ServerError", "OverloadedError"}
_STORED = re.compile(r"[A-Za-z][A-Za-z0-9_:.-]{0,80}")
_CHAIN_LIMIT = 5


def _is_requests_transport(names: set[str]) -> bool:
    return bool(names & {"ConnectionError", "Timeout"}) and "RequestException" in names


def stored_code(value: object) -> str:
    """저장된 실패 값을 재전달 때 그대로 쓸 수 있는지 본다. 코드 모양이 아니면(과거 원문) task_failed로 바꾼다."""
    text = str(value or "")
    return text if _STORED.fullmatch(text) else "task_failed"


def failure_code(exc: BaseException) -> str:
    """예외를 사용자에게 보여도 되는 실패 코드로 바꾼다. 예외 원문은 어디에도 싣지 않는다."""
    if isinstance(exc, PipelineRunCancelledError):
        code = getattr(exc, "code", None)
        return code if isinstance(code, str) else CANCELLED
    code = getattr(exc, "code", None)
    # 우리 예외(TaskFailedError 등)만 code를 그대로 쓴다. openai·minio 같은 SDK 예외의 code는 공급사 본문 값이다.
    if isinstance(code, str) and type(exc).__module__.startswith("app.") and _CODE.fullmatch(code):
        return code
    current: BaseException | None = exc
    for _ in range(_CHAIN_LIMIT):  # SDK 예외는 다른 예외로 감싸여 올라오므로 원인 사슬을 따라간다.
        if current is None:
            break
        status = getattr(current, "status_code", None)
        if not isinstance(status, int):
            status = getattr(getattr(current, "response", None), "status_code", None)  # httpx·requests HTTPError
        if not isinstance(status, int):
            status = getattr(current, "code", None)
        if status == 429:
            return PROVIDER_RATE_LIMITED
        if isinstance(status, int) and status >= 500:
            return PROVIDER_UNAVAILABLE
        if status in (400, 413, 422):
            return INPUT_REJECTED
        names = {cls.__name__ for cls in type(current).__mro__}
        if "RateLimitError" in names:
            return PROVIDER_RATE_LIMITED
        if names & _UNAVAILABLE_NAMES or _is_requests_transport(names) or isinstance(
            current, (httpx.TransportError, TimeoutError, ConnectionError)
        ):
            return PROVIDER_UNAVAILABLE
        current = current.__cause__ or current.__context__
    if isinstance(exc, json.JSONDecodeError):  # 모델 출력 파싱 실패는 입력 문제가 아니다.
        return INTERNAL_ERROR
    if isinstance(exc, ValueError):  # pydantic ValidationError 포함
        return INPUT_REJECTED
    return INTERNAL_ERROR
