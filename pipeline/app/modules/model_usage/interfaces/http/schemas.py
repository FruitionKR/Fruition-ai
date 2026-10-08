from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, NonNegativeFloat, NonNegativeInt


class ModelUsage(BaseModel):
    provider: str
    requested_model: str
    model: str
    calls: NonNegativeInt
    failed_calls: NonNegativeInt
    unfinished_calls: NonNegativeInt
    unknown_usage_calls: NonNegativeInt
    known_input_tokens: NonNegativeInt
    known_output_tokens: NonNegativeInt
    known_cached_input_tokens: NonNegativeInt
    known_cache_creation_tokens: NonNegativeInt
    known_reasoning_tokens: NonNegativeInt
    unknown_cache_usage_calls: NonNegativeInt


class ModelUsageResponse(BaseModel):
    workspace_id: str
    user_id: str
    from_at: datetime
    to_at: datetime
    models: list[ModelUsage]


class ModelUsageCall(BaseModel):
    id: UUID
    run_id: str
    workspace_id: str
    user_id: str
    kind: str
    provider: str
    requested_model: str
    model: str
    status: Literal['started', 'succeeded', 'failed', 'abandoned']
    input_tokens: NonNegativeInt | None
    output_tokens: NonNegativeInt | None
    cached_input_tokens: NonNegativeInt | None
    cache_creation_tokens: NonNegativeInt | None
    reasoning_tokens: NonNegativeInt | None
    audio_seconds: NonNegativeFloat | None
    input_characters: NonNegativeInt | None
    started_at: datetime
    finished_at: datetime | None


class ModelUsageCallsResponse(BaseModel):
    calls: list[ModelUsageCall]
