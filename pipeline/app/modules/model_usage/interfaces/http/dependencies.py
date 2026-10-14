from typing import Annotated

from fastapi import Header

from app.modules.model_usage.application.read_usage import ListUsageCallsUseCase, ReadUsageUseCase
from app.modules.model_usage.infrastructure.usage_ledger import PostgresUsageRepository


def get_read_usage():
    return ReadUsageUseCase(PostgresUsageRepository())


def get_list_usage_calls():
    return ListUsageCallsUseCase(PostgresUsageRepository())


# document가 HTTP 동기 호출마다 보내는 요청 ID. 사용량 원장의 run_id가 된다.
RequestId = Annotated[str | None, Header(alias='X-Request-Id', max_length=128)]
