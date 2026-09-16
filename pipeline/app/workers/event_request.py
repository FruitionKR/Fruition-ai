import re
from typing import Any


_SECRET_FIELD_PARTS = {"token", "password", "secret", "endpoint"}


def without_top_level_secrets(command: dict[str, Any]) -> dict[str, Any]:
    return _scrub_secrets(command)


def _scrub_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _scrub_secrets(item)
            for key, item in value.items()
            if not _is_secret_field(key)
        }
    if isinstance(value, list):
        return [_scrub_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_secrets(item) for item in value)
    return value


def _is_secret_field(name: str) -> bool:
    normalized = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])",
        "_",
        name,
    ).lower().replace("-", "_")
    parts = set(normalized.split("_"))
    return (
        normalized == "api_key"
        or normalized.endswith("_api_key")
        or normalized in {"api_base_url", "base_url"}
        or not parts.isdisjoint(_SECRET_FIELD_PARTS)
    )
