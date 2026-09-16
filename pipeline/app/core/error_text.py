from __future__ import annotations


def truncate_error(error: object, limit: int = 240) -> str:
    text = str(error)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
