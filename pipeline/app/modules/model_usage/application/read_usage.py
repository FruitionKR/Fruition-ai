"""사용자별 모델 사용량 조회 계약."""
from datetime import datetime
from typing import Any, Protocol


class UsageRepositoryPort(Protocol):
    def summarize(self, workspace_id: str, user_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]: ...
    def list_calls(self, run_id: str | None, start: datetime | None, end: datetime | None) -> list[dict[str, Any]]: ...


def _validate_period(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError('조회 기간은 시간대를 포함하고 시작이 종료보다 앞서야 합니다.')


class ReadUsageUseCase:
    def __init__(self, repository: UsageRepositoryPort):
        self.repository = repository

    def execute(self, workspace_id: str, user_id: str, start: datetime, end: datetime) -> dict[str, Any]:
        _validate_period(start, end)
        return {'workspace_id': workspace_id, 'user_id': user_id, 'from_at': start, 'to_at': end,
                'models': self.repository.summarize(workspace_id, user_id, start, end)}


class ListUsageCallsUseCase:
    """document가 호출 시점 단가를 적용하도록 호출 단위 행을 돌려준다."""

    def __init__(self, repository: UsageRepositoryPort):
        self.repository = repository

    def execute(self, run_id: str | None, start: datetime | None, end: datetime | None) -> dict[str, Any]:
        if run_id is not None and (start is not None or end is not None):
            raise ValueError('run_id와 조회 기간은 함께 지정할 수 없습니다.')
        if run_id is None:
            if start is None or end is None:
                raise ValueError('run_id 또는 finished_from·finished_to를 지정해야 합니다.')
            _validate_period(start, end)
        return {'calls': self.repository.list_calls(run_id, start, end)}
