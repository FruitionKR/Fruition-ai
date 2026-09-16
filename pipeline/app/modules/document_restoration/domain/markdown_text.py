from __future__ import annotations


def strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[-1].startswith("```"):
        return "\n".join(lines[1:-1]).strip()
    return stripped


def is_valid_markdown_table(text: str) -> bool:
    rows = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if len(rows) < 2:
        return False
    if len({row.count("|") for row in rows}) != 1:
        return False
    separator = rows[1].replace("|", "").replace(":", "").replace("-", "").strip()
    return not separator


def has_balanced_braces(text: str) -> bool:
    depth = 0
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0
