from datetime import datetime
from pydantic import BaseModel, NonNegativeInt


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
