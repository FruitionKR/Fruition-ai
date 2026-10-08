from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from app.modules.model_usage.application.read_usage import ListUsageCallsUseCase, ReadUsageUseCase
from app.modules.model_usage.interfaces.http.dependencies import get_list_usage_calls, get_read_usage
from app.modules.model_usage.interfaces.http.schemas import ModelUsageCallsResponse, ModelUsageResponse

router = APIRouter(prefix='/usage', tags=['model-usage'])
internal_router = APIRouter(prefix='/internal/model-usage', tags=['model-usage'])


@router.get('/models', response_model=ModelUsageResponse)
def read_usage(
    workspace_id: Annotated[str, Query(min_length=1)],
    user_id: Annotated[str, Query(min_length=1)],
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    use_case: ReadUsageUseCase = Depends(get_read_usage),
):
    now = datetime.now(timezone.utc)
    start = from_at or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        return use_case.execute(workspace_id, user_id, start, to_at or now)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@internal_router.get('/calls', response_model=ModelUsageCallsResponse)
def list_usage_calls(
    run_id: Annotated[str | None, Query(min_length=1)] = None,
    finished_from: datetime | None = None,
    finished_to: datetime | None = None,
    use_case: ListUsageCallsUseCase = Depends(get_list_usage_calls),
):
    try:
        return use_case.execute(run_id, finished_from, finished_to)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
