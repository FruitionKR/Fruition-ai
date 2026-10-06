"""TypeSafe Jev choice 판단 호출.

Jev는 기능별 설정을 켜고 `TYPESAFE_API_KEY`가 있을 때만 쓴다. 호출할 수 없거나 응답이
계약과 다르면 `JevUnavailable`을 던져 호출자가 기존 경로로 처리하게 한다.
"""

from __future__ import annotations

import logging
import os
import time
from email.utils import parsedate_to_datetime
from functools import lru_cache
from threading import Lock
from types import SimpleNamespace
from typing import Any

import httpx

from app.core.llm_env import first_env, float_env
from app.core.llm_prompt import redact_numeric_personal_data, with_llm_security_boundary
from app.core.pipeline_control import ensure_task_active
from app.modules.model_usage.infrastructure.usage_ledger import track_call

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.13.0"
JEV_PROVIDER = "typesafe"
JEV_API_KEY_ENV = "TYPESAFE_API_KEY"
JEV_ROUTING_ENABLED_ENV = "JEV_ROUTING_ENABLED"
JEV_EVIDENCE_ENABLED_ENV = "JEV_EVIDENCE_ENABLED"
JEV_CONCEPT_MERGE_ENABLED_ENV = "JEV_CONCEPT_MERGE_ENABLED"
_RETRY_STATUSES = {429, 502, 503, 504, 529}
# 크레딧 소진(402)·키 오류(401/403)는 재시도해도 풀리지 않으므로 일정 시간 Jev 호출을 건너뛴다.
_BLOCKING_STATUSES = {401, 402, 403}
_MAX_ATTEMPTS = 4
_MAX_RETRY_AFTER_SECONDS = 60.0
# 사용자가 기다리는 경로(Agent 라우팅·Query 근거 선택)의 호출 전체 응답 예산. 넘으면 바로 기존 경로로 간다.
INTERACTIVE_DEADLINE_SECONDS = 15.0
_TRUE_VALUES = {"1", "true", "yes", "on"}
logger = logging.getLogger(__name__)
_blocked_until = 0.0
_blocked_lock = Lock()


class JevUnavailable(RuntimeError):
    """Jev를 쓸 수 없어 기존 경로로 처리해야 한다."""


def build_jev_client(feature_env: str, *, interactive: bool = False) -> JevClient | None:
    """기능 설정이 켜져 있고 API 키가 있으면 클라이언트를 만든다.

    `interactive`면 호출 전체를 `INTERACTIVE_DEADLINE_SECONDS` 안에 끝내고, 타임아웃은 재시도하지 않는다.
    """
    if os.environ.get(feature_env, "false").strip().lower() not in _TRUE_VALUES:
        return None
    api_key = first_env((JEV_API_KEY_ENV,), strip=True)
    if not api_key:
        return None
    deadline = INTERACTIVE_DEADLINE_SECONDS if interactive else None
    return _shared_client(api_key, float_env("JEV_TIMEOUT_SECONDS", 30.0), deadline)


@lru_cache(maxsize=2)
def _shared_client(api_key: str, timeout_seconds: float, deadline_seconds: float | None) -> JevClient:
    # 작업마다 use case를 새로 만들므로 연결 풀을 프로세스 안에서 공유한다.
    return JevClient(api_key, timeout_seconds=timeout_seconds, deadline_seconds=deadline_seconds)


class JevClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 30.0,
        deadline_seconds: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._deadline_seconds = deadline_seconds
        self._http = http or httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    def choose(self, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """질문별 choice 응답을 돌려준다. 선택지는 각 질문의 `criteria` 키다."""
        _raise_if_blocked()
        ensure_task_active()
        payload = {
            "model": JEV_MODEL,
            "state": redact_numeric_personal_data(state),
            "questions": {
                key: {**question, "instructions": with_llm_security_boundary(question["instructions"])}
                for key, question in questions.items()
            },
        }
        with track_call(JEV_PROVIDER, JEV_MODEL) as receipt:
            data = self._post(payload)
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            receipt["response"] = SimpleNamespace(
                usage_metadata=usage,
                response_metadata={"model": data.get("model")},
            )
        ensure_task_active()
        return _validated_answers(data, questions)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        # 평가 실행기(jev_api.RetryingClient)와 같이 일시 오류는 최대 4회까지 2·4·8초 간격으로 시도한다.
        # 응답 예산이 있으면(실시간 경로) 타임아웃은 재시도하지 않고, 예산을 넘는 대기 없이 바로 포기한다.
        started = time.monotonic()
        for attempt in range(_MAX_ATTEMPTS):
            last = attempt == _MAX_ATTEMPTS - 1
            remaining = self._remaining(started)
            try:
                response = self._http.post(
                    JEV_ENDPOINT,
                    json=payload,
                    timeout=self._timeout_seconds if remaining is None else min(self._timeout_seconds, remaining),
                )
            except httpx.TimeoutException:
                if last or self._deadline_seconds is not None:
                    raise JevUnavailable("Jev 응답 시간 초과") from None
                time.sleep(2 ** (attempt + 1))
                continue
            except httpx.HTTPError as exc:
                raise JevUnavailable(f"Jev 전송 실패: {type(exc).__name__}") from None
            if response.status_code in _BLOCKING_STATUSES:
                _block(response.status_code)
                raise JevUnavailable(f"Jev HTTP {response.status_code}")
            if response.status_code in _RETRY_STATUSES and not last:
                delay = _retry_delay(response, attempt)
                if delay is None:
                    raise JevUnavailable(f"Jev HTTP {response.status_code}, 재시도 대기가 너무 깁니다.")
                remaining = self._remaining(started)
                if remaining is not None and delay >= remaining:
                    raise JevUnavailable(f"Jev HTTP {response.status_code}, 재시도 대기가 응답 예산을 넘습니다.")
                time.sleep(delay)
                continue
            if response.status_code != 200:
                raise JevUnavailable(f"Jev HTTP {response.status_code}")
            try:
                data = response.json()
            except ValueError:
                raise JevUnavailable("Jev 응답이 JSON이 아닙니다.") from None
            if not isinstance(data, dict):
                raise JevUnavailable("Jev 응답이 객체가 아닙니다.")
            return data
        raise JevUnavailable("Jev 재시도 한도를 넘었습니다.")

    def _remaining(self, started: float) -> float | None:
        if self._deadline_seconds is None:
            return None
        remaining = self._deadline_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise JevUnavailable("Jev 응답 예산을 넘었습니다.")
        return remaining


def _validated_answers(
    data: dict[str, Any],
    questions: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    answers = data.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise JevUnavailable("Jev 응답의 질문 목록이 요청과 다릅니다.")
    for key, answer in answers.items():
        if not isinstance(answer, dict) or answer.get("choice") not in questions[key]["criteria"]:
            raise JevUnavailable(f"Jev 응답의 선택지가 요청에 없습니다: {key}")
    return answers


def choice_probability(answer: dict[str, Any], choice: str) -> float:
    probability = (answer.get("probabilities") or {}).get(choice)
    if isinstance(probability, (int, float)) and not isinstance(probability, bool) and 0.0 <= probability <= 1.0:
        return float(probability)
    return 0.0


def _retry_delay(response: httpx.Response, attempt: int) -> float | None:
    retry_after = response.headers.get("retry-after", "0")
    try:
        delay = float(retry_after)
    except ValueError:
        try:
            delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            delay = 0.0
    if delay > _MAX_RETRY_AFTER_SECONDS:
        return None
    return max(float(2 ** (attempt + 1)), delay)


def _block(status_code: int) -> None:
    global _blocked_until
    seconds = float_env("JEV_BLOCK_SECONDS", 600.0)
    with _blocked_lock:
        _blocked_until = time.monotonic() + seconds
    logger.warning("[Jev 중지] HTTP %s, %.0f초 동안 기존 경로를 사용합니다.", status_code, seconds)


def _raise_if_blocked() -> None:
    with _blocked_lock:
        blocked = time.monotonic() < _blocked_until
    if blocked:
        raise JevUnavailable("Jev가 크레딧·인증 오류로 일시 중지됐습니다.")
