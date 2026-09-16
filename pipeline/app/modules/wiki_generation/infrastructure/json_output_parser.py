from __future__ import annotations

import json
import re
from typing import Any, Dict

JsonDict = Dict[str, Any]


class JsonParseError(RuntimeError):
    pass


class SectionPolishParseError(JsonParseError):
    def __init__(self, message: str, raw_content: str) -> None:
        super().__init__(message)
        self.raw_content = raw_content


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


def parse_section_polish_object(content: str) -> JsonDict:
    cleaned = strip_json_fence(content)
    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(cleaned[start : end + 1])

    last_error: Exception | None = None
    for candidate in candidates:
        for repaired in _section_polish_repair_candidates(candidate):
            try:
                value = json.loads(repaired)
            except json.JSONDecodeError as exc:
                last_error = exc
                continue
            if not isinstance(value, dict):
                last_error = JsonParseError("SectionPolish output must be a JSON object")
                continue
            return _normalize_section_polish_schema(value)
    raise SectionPolishParseError(f"SectionPolish output is not repairable JSON: {last_error}", content)


def _section_polish_repair_candidates(text: str) -> list[str]:
    out = []
    current = text.strip()
    out.append(current)
    current = re.sub(r",\s*([}\]])", r"\1", current)
    out.append(current)
    current = current.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    out.append(current)
    out.append(_escape_invalid_json_backslashes(current))
    return out


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


def _normalize_section_polish_schema(value: JsonDict) -> JsonDict:
    items = value.get("items", [])
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []

    normalized_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized_items.append(
            {
                "text": str(item.get("text", "")),
                "anchor_block_ids": _as_string_list(item.get("anchor_block_ids", [])),
            }
        )

    return {
        "section": str(value.get("section", "")),
        "title": str(value.get("title", "")),
        "text": str(value.get("text", "")),
        "anchor_block_ids": _as_string_list(value.get("anchor_block_ids", [])),
        "items": normalized_items,
        "related_concept_hints": _as_string_list(value.get("related_concept_hints", [])),
        "confidence": _as_float(value.get("confidence", 0.0)),
    }


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0
