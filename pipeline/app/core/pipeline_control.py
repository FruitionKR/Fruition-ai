import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class PipelineRunCancelledError(RuntimeError):
    """취소 요청 또는 실행 대상 비활성화로 pipeline을 중단한다."""


class ScopePurgedError(PipelineRunCancelledError):
    """파기된 워크스페이스·사용자의 작업이다. 재시도해도 같으므로 실패로 확정한다."""


class DeadlineExceededError(PipelineRunCancelledError):
    """호출자가 기다리는 시간이 지났다. 결과를 저장하지 않고 실패로 끝낸다."""

    code = "deadline_exceeded"


class ReplayedResult(dict):
    """이미 저장된 결과를 재전달 때 그대로 돌려준 것이다. 새로 실행한 결과와 구분해 부수 효과(제목 생성 등)를 건너뛴다."""


task_run_id: ContextVar[str | None] = ContextVar("ai_task_run_id", default=None)


_active_check: ContextVar[Callable[[], bool | None] | None] = ContextVar("active_task_check", default=None)


# document가 60초에 포기하는 동기 경로(Skill 게시·수정, 위키 스키마 preview·drafts)의 ai 전체 마감.
DOCUMENT_SYNC_DEADLINE_SECONDS = 55

# 요청 전체 마감(time.monotonic 기준). 호출자(document·ALB)가 포기한 뒤에도 모델 호출과 저장이 이어지지 않게 한다.
_deadline: ContextVar[float | None] = ContextVar("request_deadline", default=None)


@contextmanager
def request_deadline(seconds: float) -> Iterator[None]:
    parent = _deadline.get()
    deadline = time.monotonic() + seconds
    token = _deadline.set(deadline if parent is None else min(parent, deadline))
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_seconds() -> float | None:
    """마감이 없으면 None, 있으면 남은 초(지났으면 0 이하)."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def ensure_task_active() -> None:
    remaining = remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise DeadlineExceededError("Request deadline exceeded.")
    check = _active_check.get()
    if check is not None and check() is False:
        raise PipelineRunCancelledError("Task cancellation requested.")


@contextmanager
def task_cancellation_scope(check: Callable[[], bool | None]) -> Iterator[None]:
    parent = _active_check.get()
    def combined() -> bool:
        return (parent is None or parent() is not False) and check() is not False
    token = _active_check.set(combined)
    try:
        yield
    finally:
        _active_check.reset(token)
