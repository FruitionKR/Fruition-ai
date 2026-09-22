from __future__ import annotations

import json
import re
from typing import Any, Dict

JsonDict = Dict[str, Any]


class JsonParseError(RuntimeError):
    pass


def strip_json_fence(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


def parse_json_object(content: str) -> JsonDict:
    cleaned = strip_json_fence(content)
    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(cleaned[start : end + 1])

    last_error: Exception | None = None
    for candidate in candidates:
        for repaired in _json_repair_candidates(candidate):
            try:
                value = json.loads(repaired)
            except json.JSONDecodeError as exc:
                last_error = exc
                try:
                    value, _ = json.JSONDecoder().raw_decode(repaired)
                except json.JSONDecodeError:
                    continue
            if not isinstance(value, dict):
                last_error = JsonParseError("Model output must be a JSON object")
                continue
            return value
    if isinstance(last_error, JsonParseError):
        raise last_error
    raise JsonParseError(f"Model output is not repairable JSON: {last_error}")


def _json_repair_candidates(text: str) -> list[str]:
    current = text.strip()
    candidates = [current]
    current = re.sub(r",\s*([}\]])", r"\1", current)
    candidates.append(current)
    current = current.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    candidates.append(current)
    candidates.append(_escape_invalid_json_backslashes(current))
    return list(dict.fromkeys(candidates))


def _escape_invalid_json_backslashes(text: str) -> str:
    return re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)
