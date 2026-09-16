from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class PipelineRunCancelledError(RuntimeError):
    """취소 요청 또는 실행 대상 비활성화로 pipeline을 중단한다."""


task_run_id: ContextVar[str | None] = ContextVar("ai_task_run_id", default=None)


_active_check: ContextVar[Callable[[], bool | None] | None] = ContextVar("active_task_check", default=None)


def ensure_task_active() -> None:
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
