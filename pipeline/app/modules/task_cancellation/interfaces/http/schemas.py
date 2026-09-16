from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CancelTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    command: dict[str, Any] | None = None


class TaskStatusResponse(BaseModel):
    id: str
    status: str
    error_code: str | None = None




class DocumentCleanupResponse(BaseModel):
    cleanup_complete: bool
