"""사용자별 모델 사용량 조회 계약."""
from datetime import datetime
from typing import Any, Protocol


class UsageRepositoryPort(Protocol):
    def summarize(self, workspace_id: str, user_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]: ...


class ReadUsageUseCase:
    def __init__(self, repository: UsageRepositoryPort):
        self.repository = repository

    def execute(self, workspace_id: str, user_id: str, start: datetime, end: datetime) -> dict[str, Any]:
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError('조회 기간은 시간대를 포함하고 시작이 종료보다 앞서야 합니다.')
        return {'workspace_id': workspace_id, 'user_id': user_id, 'from_at': start, 'to_at': end,
                'models': self.repository.summarize(workspace_id, user_id, start, end)}
