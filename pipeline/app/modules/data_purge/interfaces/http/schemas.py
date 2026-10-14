from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class PurgeWorkspacesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_ids: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class PurgeUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1)


class PurgeResponse(BaseModel):
    deleted: dict[str, int]
