from __future__ import annotations

import re
from dataclasses import dataclass

from app.modules.markdown_edit.domain.entities import MarkdownEditOperation, MarkdownEditTarget


@dataclass(frozen=True)
class MarkdownTargetScope:
    markdown: str
    context_before: str
    context_after: str
    start_line: int
    end_line: int


class MarkdownTargetBoundaryError(ValueError):
    def __init__(self, structure: str, start_line: int, end_line: int) -> None:
        super().__init__(f"target partially overlaps {structure} at lines {start_line}-{end_line}")
        self.structure = structure
        self.start_line = start_line
        self.end_line = end_line


def build_markdown_target_scope(
    markdown: str,
    target: MarkdownEditTarget,
    context_lines: int,
) -> MarkdownTargetScope:
    lines = re.split(r"\r\n|\r|\n", markdown)
    if target.end_line > len(lines):
        raise ValueError("target.end_line must not exceed the Markdown line count.")

    if target.type == "whole_document":
        return MarkdownTargetScope(
            markdown=markdown,
            context_before="",
            context_after="",
            start_line=1,
            end_line=len(lines),
        )

    start_index = target.start_line - 1
    end_index = target.end_line
    context_lines = max(0, context_lines)
    context_start = max(0, start_index - context_lines)
    context_end = min(len(lines), end_index + context_lines)
    return MarkdownTargetScope(
        markdown=markdown_line_range(markdown, target.start_line, target.end_line),
        context_before=(
            markdown_line_range(markdown, context_start + 1, start_index)
            if context_start < start_index
            else ""
        ),
        context_after=(
            markdown_line_range(markdown, end_index + 1, context_end)
            if end_index < context_end
            else ""
        ),
        start_line=context_start + 1,
        end_line=context_end,
    )


def markdown_line_count(markdown: str) -> int:
    return len(re.split(r"\r\n|\r|\n", markdown))


def markdown_line_range(markdown: str, start_line: int, end_line: int) -> str:
    separators = list(re.finditer(r"\r\n|\r|\n", markdown))
    line_starts = [0, *(match.end() for match in separators)]
    start_index = line_starts[start_line - 1]
    end_index = separators[end_line - 1].start() if end_line <= len(separators) else len(markdown)
    return markdown[start_index:end_index]


def apply_markdown_edit(markdown: str, edit: MarkdownEditOperation) -> str:
    newline_match = re.search(r"\r\n|\r|\n", markdown)
    newline = newline_match.group() if newline_match else "\n"
    lines = re.split(r"\r\n|\r|\n", markdown)
    replacement = re.split(r"\r\n|\r|\n", edit.replacement_markdown)
    target = edit.actual_target
    if edit.operation == "replace":
        lines[target.start_line - 1:target.end_line] = replacement
    else:
        lines[target.end_line:target.end_line] = replacement
    return newline.join(lines)
