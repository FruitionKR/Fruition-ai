from collections import Counter

from fastapi import APIRouter

from app.modules.data_purge.infrastructure import postgres_purge as purge
from app.modules.data_purge.interfaces.http.schemas import PurgeResponse, PurgeUserRequest, PurgeWorkspacesRequest

router = APIRouter(prefix="/internal/ai/purge", tags=["ai-data-purge"])


# 실패는 500으로 그대로 올려 document가 재시도하게 한다. 이미 지운 범위는 재호출 때 0건으로 지나간다.
@router.post("/workspaces", response_model=PurgeResponse)
def purge_workspaces(request: PurgeWorkspacesRequest):
    deleted = Counter()
    for workspace_id in dict.fromkeys(request.workspace_ids):
        deleted.update(purge.purge_workspace(workspace_id))
    return {"deleted": dict(deleted)}


@router.post("/users", response_model=PurgeResponse)
def purge_user(request: PurgeUserRequest):
    return {"deleted": purge.purge_user(request.user_id)}
