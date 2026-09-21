from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from app.modules.model_usage.application.read_usage import ReadUsageUseCase
from app.modules.model_usage.interfaces.http.dependencies import get_read_usage
from app.modules.model_usage.interfaces.http.schemas import ModelUsageResponse

router = APIRouter(prefix='/usage', tags=['model-usage'])


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
