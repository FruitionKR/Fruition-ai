from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

from app.modules.markdown_edit.domain.entities import MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.domain.markdown_output_contract import TASK_LIST_MARKER_PATTERN
from app.modules.markdown_edit.domain.markdown_target_scope import MarkdownTargetBoundaryError


SOURCE_RANGE_EDIT_GOALS = {"cleanup", "style_change", "translate"}
STRUCTURE_ACTION_WORDS = ("추가", "변경", "바꿔", "변환", "표시", "만들", "change", "convert", "format")
STRUCTURE_TARGET_WORDS = (
    "목록",
    "리스트",
    "checklist",
    "heading",
    "제목",
    "굵게",
    "bold",
    "기울임",
    "italic",
    "취소선",
    "strikethrough",
    "인용",
    "blockquote",
    "표",
    "table",
    "code block",
    "코드 블록",
    "수식",
    "math",
    "mermaid",
    "회의록",
    "checkbox",
    "체크박스",
    "task list",
)
LOCKED_BLOCK_TYPES = {"code_block", "fence", "html_block", "hr", "table_open"}
BOUNDARY_BLOCK_TYPES = {"code_block", "fence", "html_block", "table_open"}


@dataclass(frozen=True)
class MarkdownTextSegment:
    id: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class MarkdownSourceRangePlan:
    markdown: str
    masked_markdown: str
    segments: tuple[MarkdownTextSegment, ...]
    required_segment_ids: tuple[str, ...]
    structure_fingerprint: tuple[tuple[object, ...], ...]

    def apply(self, replacements: dict[str, str]) -> str:
        segment_by_id = {segment.id: segment for segment in self.segments}
        edited = self.markdown
        for segment_id, replacement in sorted(
            replacements.items(),
            key=lambda item: segment_by_id[item[0]].start,
            reverse=True,
        ):
            segment = segment_by_id[segment_id]
            edited = f"{edited[:segment.start]}{replacement}{edited[segment.end:]}"
        return edited


def validate_markdown_target_boundary(markdown: str, target: MarkdownEditTarget) -> None:
    if target.type == "whole_document":
        return

    protected_ranges: list[tuple[str, int, int]] = []
    for token in _markdown_parser().parse(markdown):
        if token.type in BOUNDARY_BLOCK_TYPES and token.map is not None:
            protected_ranges.append((token.type, token.map[0] + 1, token.map[1]))

    patterns = (
        ("frontmatter", r"(?s)\A---\r?\n.*?\r?\n---(?=\r?\n|\Z)"),
        ("footnote_definition", r"(?m)^\[\^[^\]\n]+\]:[^\n]*(?:\r?\n(?: {2,}|\t)[^\n]*)+"),
        ("display_math", r"(?ms)^\$\$[ \t]*\r?$.*?^\$\$[ \t]*\r?$"),
    )
    for structure, pattern in patterns:
        for match in re.finditer(pattern, markdown):
            start_line = markdown.count("\n", 0, match.start()) + 1
            end_line = markdown.count("\n", 0, match.end()) + 1
            protected_ranges.append((structure, start_line, end_line))

    for structure, start_line, end_line in protected_ranges:
        overlaps = target.start_line <= end_line and target.end_line >= start_line
        contains = target.start_line <= start_line and target.end_line >= end_line
        if overlaps and not contains:
            raise MarkdownTargetBoundaryError(structure, start_line, end_line)


def build_source_range_plan(request: MarkdownEditRequest) -> MarkdownSourceRangePlan | None:
    if request.edit_operation != "replace" or not _supports_source_range_edit(request):
        return None

    parser = _markdown_parser()
    tokens = parser.parse(request.markdown)
    line_starts = _line_starts(request.markdown)
    translate = request.edit_goal == "translate"
    locked_block_ranges = _locked_block_ranges(request.markdown, tokens, line_starts, translate)
    locked_ranges = _merge_ranges([*locked_block_ranges, *_locked_inline_ranges(request.markdown, translate)])
    segments: list[MarkdownTextSegment] = []

    for token_index, token in enumerate(tokens):
        if token.type != "inline" or token.map is None or not token.children:
            continue
        if not translate and token_index > 0 and tokens[token_index - 1].type == "heading_open":
            continue
        block_start = line_starts[token.map[0]]
        block_end = line_starts[token.map[1]] if token.map[1] < len(line_starts) else len(request.markdown)
        if _overlaps_any(block_start, block_end, locked_block_ranges):
            continue
        cursor = block_start
        for child in token.children:
            if child.type != "text" or not child.content:
                continue
            pieces, found_end = _find_unlocked_text_pieces(
                request.markdown,
                child.content,
                cursor,
                block_end,
                locked_ranges,
            )
            if found_end is None:
                return None
            cursor = found_end
            for start, end in pieces:
                start, end = _trim_whitespace(request.markdown, start, end)
                if start == end or not any(character.isalnum() for character in request.markdown[start:end]):
                    continue
                if start > 0 and request.markdown[start - 1] == "\\":
                    continue
                segment_id = f"text-{len(segments) + 1:04d}"
                segments.append(
                    MarkdownTextSegment(
                        id=segment_id,
                        start=start,
                        end=end,
                        text=request.markdown[start:end],
                    )
                )

    if not segments:
        return None

    masked = request.markdown
    for segment in reversed(segments):
        token = f"{{{{FRUITION_{segment.id.upper().replace('-', '_')}}}}}"
        if token in request.markdown:
            return None
        masked = f"{masked[:segment.start]}{token}{masked[segment.end:]}"

    return MarkdownSourceRangePlan(
        markdown=request.markdown,
        masked_markdown=masked,
        segments=tuple(segments),
        required_segment_ids=_required_segment_ids(request, segments),
        structure_fingerprint=_structure_fingerprint(tokens),
    )


def apply_source_range_response(
    plan: MarkdownSourceRangePlan,
    raw_edits: object,
) -> tuple[str, list[str]]:
    failures: list[str] = []
    if not isinstance(raw_edits, list):
        return plan.markdown, ["source range response must contain an `edits` array"]

    valid_ids = {segment.id for segment in plan.segments}
    replacements: dict[str, str] = {}
    for index, raw_edit in enumerate(raw_edits):
        if not isinstance(raw_edit, dict):
            failures.append(f"source range edit at index {index} must be an object")
            continue
        segment_id = str(raw_edit.get("id") or "").strip()
        replacement = raw_edit.get("replacement")
        if segment_id not in valid_ids:
            failures.append(f"unknown source range segment id: {segment_id or '<empty>'}")
            continue
        if segment_id in replacements:
            failures.append(f"duplicate source range segment id: {segment_id}")
            continue
        if not isinstance(replacement, str) or not replacement.strip():
            failures.append(f"source range replacement must be non-empty text: {segment_id}")
            continue
        if "\n" in replacement or "\r" in replacement:
            failures.append(f"source range replacement must stay on one line: {segment_id}")
            continue
        if re.search(r"https?://", replacement):
            failures.append(f"source range replacement must not contain a URL: {segment_id}")
            continue
        replacements[segment_id] = replacement

    for segment_id in plan.required_segment_ids:
        if segment_id not in replacements:
            failures.append(f"required source range segment is missing: {segment_id}")

    if failures:
        return plan.markdown, failures

    edited = plan.apply(replacements)
    if _structure_fingerprint(_markdown_parser().parse(edited)) != plan.structure_fingerprint:
        return edited, ["source range edits must not change Markdown structure"]
    return edited, []


def source_range_payload(plan: MarkdownSourceRangePlan) -> dict[str, object]:
    return {
        "mode": "source_range_text_edit",
        "markdown_context": plan.masked_markdown,
        "segments": [{"id": segment.id, "text": segment.text} for segment in plan.segments],
        "required_segment_ids": list(plan.required_segment_ids),
    }


def _required_segment_ids(
    request: MarkdownEditRequest,
    segments: list[MarkdownTextSegment],
) -> tuple[str, ...]:
    if request.edit_goal != "translate":
        return ()
    instruction = request.instruction.lower()
    if "한국어" in instruction or "korean" in instruction:
        return tuple(segment.id for segment in segments if re.search(r"[A-Za-z]", segment.text))
    if "영어" in instruction or "english" in instruction:
        return tuple(segment.id for segment in segments if re.search(r"[가-힣]", segment.text))
    return tuple(segment.id for segment in segments)


def _supports_source_range_edit(request: MarkdownEditRequest) -> bool:
    if request.edit_goal not in SOURCE_RANGE_EDIT_GOALS:
        return False
    instruction = request.instruction.lower()
    asks_for_structure = any(word in instruction for word in STRUCTURE_TARGET_WORDS) and any(
        word in instruction for word in STRUCTURE_ACTION_WORDS
    )
    return not asks_for_structure


def _markdown_parser() -> MarkdownIt:
    return MarkdownIt("commonmark").enable("table")


def _line_starts(markdown: str) -> list[int]:
    return [0, *(match.end() for match in re.finditer(r"\n", markdown))]


def _locked_block_ranges(
    markdown: str,
    tokens: list[Token],
    line_starts: list[int],
    translate: bool,
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for token in tokens:
        if token.type not in LOCKED_BLOCK_TYPES or token.map is None:
            continue
        if translate and token.type == "table_open":
            continue
        start = line_starts[token.map[0]]
        end = line_starts[token.map[1]] if token.map[1] < len(line_starts) else len(markdown)
        ranges.append((start, end))

    frontmatter = re.match(r"(?s)\A---\r?\n.*?\r?\n---(?=\r?\n|\Z)", markdown)
    if frontmatter:
        ranges.append(frontmatter.span())
    return _merge_ranges(ranges)


def _locked_inline_ranges(markdown: str, translate: bool) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    footnote_definition = (
        r"(?m)^\[\^[^\]\n]+\]:[ \t]*"
        if translate
        else r"(?m)^\[\^[^\]\n]+\]:[^\n]*(?:\r?\n(?: {2,}|\t)[^\n]*)*"
    )
    patterns = (
        footnote_definition,
        r"\[\^[^\]\n]+\]",
        r"!\[[^\]\n]*\]\([^\)\n]+\)",
        r"`+[^`\n]+`+",
        r"<[^>\n]+>",
        r"(?s)\$\$.*?\$\$",
        TASK_LIST_MARKER_PATTERN,
    )
    for pattern in patterns:
        ranges.extend(match.span() for match in re.finditer(pattern, markdown))

    if translate:
        for match in re.finditer(r"(?<!!)\[[^\]\n]+\]\(([^\)\n]+)\)", markdown):
            ranges.append(match.span(1))
        for match in re.finditer(r"\[[^\]\n]+\](\[[^\]\n]*\])", markdown):
            ranges.append(match.span(1))
    else:
        ranges.extend(match.span() for match in re.finditer(r"(?<!!)\[[^\]\n]+\]\([^\)\n]+\)", markdown))
        ranges.extend(match.span() for match in re.finditer(r"\[[^\]\n]+\]\[[^\]\n]*\]", markdown))

    if not translate and re.search(r"[가-힣]", markdown):
        ranges.extend(
            match.span()
            for match in re.finditer(
                r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_.:/+-]*(?:[ \t]+[A-Za-z][A-Za-z0-9_.:/+-]*)*(?![A-Za-z0-9_])",
                markdown,
            )
        )

    return _merge_ranges(ranges)


def _merge_ranges(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _find_unlocked_text_pieces(
    markdown: str,
    text: str,
    start: int,
    end: int,
    locked_ranges: tuple[tuple[int, int], ...],
) -> tuple[list[tuple[int, int]], int | None]:
    cursor = start
    last_found_end: int | None = None
    while cursor < end:
        found = markdown.find(text, cursor, end)
        if found < 0:
            return [], last_found_end
        found_end = found + len(text)
        last_found_end = found_end
        pieces = _subtract_ranges(found, found_end, locked_ranges)
        if pieces:
            return pieces, found_end
        cursor = found_end
    return [], last_found_end


def _subtract_ranges(
    start: int,
    end: int,
    locked_ranges: tuple[tuple[int, int], ...],
) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    cursor = start
    for locked_start, locked_end in locked_ranges:
        if locked_end <= cursor or locked_start >= end:
            continue
        if locked_start > cursor:
            pieces.append((cursor, min(locked_start, end)))
        cursor = max(cursor, locked_end)
        if cursor >= end:
            break
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def _trim_whitespace(markdown: str, start: int, end: int) -> tuple[int, int]:
    while start < end and markdown[start].isspace():
        start += 1
    while end > start and markdown[end - 1].isspace():
        end -= 1
    return start, end


def _overlaps_any(start: int, end: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start < range_end and end > range_start for range_start, range_end in ranges)


def _structure_fingerprint(tokens: list[Token]) -> tuple[tuple[object, ...], ...]:
    fingerprint: list[tuple[object, ...]] = []
    for token in tokens:
        if token.type == "inline":
            for child in token.children or ():
                if child.type in {"text", "softbreak", "hardbreak"}:
                    continue
                fingerprint.append(_token_fingerprint(child))
            continue
        fingerprint.append(_token_fingerprint(token))
    return tuple(fingerprint)


def _token_fingerprint(token: Token) -> tuple[object, ...]:
    attrs = tuple(sorted((str(key), repr(value)) for key, value in token.attrs.items()))
    literal = token.content if token.type in {"code_block", "fence", "html_block", "code_inline", "image"} else ""
    return token.type, token.tag, token.nesting, token.markup, token.info, attrs, literal
