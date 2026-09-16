from __future__ import annotations

import re

from markdown_it import MarkdownIt


def validate_markdown_syntax(markdown: str) -> list[str]:
    failures: list[str] = []
    try:
        tokens = _markdown_parser().parse(markdown)
    except Exception:  # pragma: no cover - markdown-it 내부 오류에 대한 안전 경계
        return ["Markdown parser rejected output"]

    if _contains_raw_html_or_mdx(tokens):
        failures.append("raw HTML and MDX are not supported")
    lines = markdown.splitlines()
    content_start, frontmatter_failures = _frontmatter_boundary(lines)
    failures.extend(frontmatter_failures)
    failures.extend(_unclosed_structure_failures(lines, content_start))
    return failures


def _markdown_parser() -> MarkdownIt:
    return MarkdownIt("commonmark").enable("table")


def _contains_raw_html_or_mdx(tokens: list[object]) -> bool:
    for token in tokens:
        token_type = getattr(token, "type", "")
        if token_type in {"html_block", "html_inline"}:
            if not _is_closed_html_comment(getattr(token, "content", "")):
                return True
            continue
        children = getattr(token, "children", None) or []
        for child in children:
            if child.type == "html_inline" and not _is_closed_html_comment(child.content):
                return True
            if child.type != "text":
                continue
            text = child.content
            if _is_mdx_esm(text):
                return True
            if re.search(r"(?<!\\)\{[A-Za-z_$][^{}\n]*\}", text):
                return True
    return False


def _is_closed_html_comment(content: str) -> bool:
    return re.fullmatch(r"<!--(?:(?!-->|<!--)[\s\S])*-->", content.strip()) is not None


def _is_mdx_esm(text: str) -> bool:
    import_declaration = re.match(
        r"""^\s*import\s+(?:(?:[\w$*{},\s]+\s+from\s+)?["'][^"'\n]+["'])\s*;?\s*$""",
        text,
    )
    export_declaration = re.match(
        r"^\s*export\s+(?:default\b|(?:const|let|var|function|class|async\s+function)\b|\{|\*)",
        text,
    )
    return import_declaration is not None or export_declaration is not None


def _frontmatter_boundary(lines: list[str]) -> tuple[int, list[str]]:
    if not lines or lines[0].strip() != "---":
        return 0, []

    first_content_line = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip()),
        None,
    )
    if first_content_line is None:
        return 0, []
    if lines[first_content_line].strip() == "---":
        return first_content_line + 1, []
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:", lines[first_content_line]):
        return 0, []

    closing_line = next(
        (
            index
            for index, line in enumerate(lines[first_content_line + 1 :], start=first_content_line + 1)
            if line.strip() == "---"
        ),
        None,
    )
    if closing_line is None:
        return len(lines), ["frontmatter opened at line 1 must be closed"]
    return closing_line + 1, []


def _unclosed_structure_failures(lines: list[str], start_index: int) -> list[str]:
    fence_character: str | None = None
    fence_length = 0
    fence_start_line = 0
    math_start_line = 0

    for index, line in enumerate(lines[start_index:], start=start_index):
        line_number = index + 1
        if fence_character is not None:
            closing_fence = re.match(r"^ {0,3}(`{3,}|~{3,})[\t ]*$", line)
            if closing_fence:
                marker = closing_fence.group(1)
                if marker[0] == fence_character and len(marker) >= fence_length:
                    fence_character = None
                    fence_length = 0
                    fence_start_line = 0
            continue

        if math_start_line:
            if line.strip() == "$$":
                math_start_line = 0
            continue

        opening_fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opening_fence:
            marker = opening_fence.group(1)
            info = opening_fence.group(2)
            if marker[0] != "`" or "`" not in info:
                fence_character = marker[0]
                fence_length = len(marker)
                fence_start_line = line_number
            continue

        if line.strip() == "$$":
            math_start_line = line_number

    failures: list[str] = []
    if fence_character is not None:
        failures.append(f"fenced code block opened at line {fence_start_line} must be closed")
    if math_start_line:
        failures.append(f"display math opened at line {math_start_line} must be closed")
    return failures
