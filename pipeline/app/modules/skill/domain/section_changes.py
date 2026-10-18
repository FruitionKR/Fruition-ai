"""Skill 테스트 실행의 편집 결과를 Markdown 섹션 단위 변경 전/후로 나눈다."""

import re
from dataclasses import dataclass

from app.modules.markdown_edit.domain.entities import MarkdownEditOperation


HEADING_PATTERN = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t#]*$")
FENCE_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
PREAMBLE_TITLE = "(제목 없음)"


@dataclass(frozen=True)
class MarkdownSection:
    title: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class SectionChange:
    section: str
    start_line: int
    end_line: int
    before: str
    after: str


def split_lines(markdown: str) -> list[str]:
    return re.split(r"\r\n|\r|\n", markdown)


def markdown_sections(lines: list[str]) -> tuple[MarkdownSection, ...]:
    """ATX 제목으로 섹션을 나눈다(1-based 줄). 코드 블록 안의 `#`은 제목으로 보지 않는다."""
    starts: list[tuple[str, int]] = []
    fence: str | None = None
    for number, line in enumerate(lines, start=1):
        fence_match = FENCE_PATTERN.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            fence = None if fence == marker else fence or marker
            continue
        heading = HEADING_PATTERN.match(line) if fence is None else None
        if heading:
            starts.append((heading.group(1).strip(), number))
    if not starts or starts[0][1] > 1:
        starts.insert(0, (PREAMBLE_TITLE, 1))
    return tuple(
        MarkdownSection(title, start, (starts[index + 1][1] - 1) if index + 1 < len(starts) else len(lines))
        for index, (title, start) in enumerate(starts)
    )


def section_changes(
    markdown: str,
    edited_markdown: str,
    edit: MarkdownEditOperation,
) -> tuple[tuple[SectionChange, ...], tuple[str, ...]]:
    """편집이 닿은 원본 섹션들을 하나의 변경 블록으로 묶고, 닿지 않은 섹션 제목을 돌려준다."""
    lines = split_lines(markdown)
    sections = tuple(section for section in markdown_sections(lines) if _has_content(lines, section))
    if not edit.changed or edited_markdown == markdown:
        return (), tuple(section.title for section in sections)
    target = edit.actual_target
    replacement_count = len(split_lines(edit.replacement_markdown))
    if edit.operation == "replace":
        touched_start, touched_end = target.start_line, target.end_line
        delta = replacement_count - (target.end_line - target.start_line + 1)
    else:
        # 뒤에 덧붙이면 원본 줄은 그대로이고 삽입 위치가 속한 섹션이 바뀐다.
        touched_start = touched_end = target.end_line
        delta = replacement_count
    touched = tuple(
        section for section in sections if section.start_line <= touched_end and section.end_line >= touched_start
    )
    if not touched:
        return (), tuple(section.title for section in sections)
    first, last = touched[0].start_line, touched[-1].end_line
    edited_lines = split_lines(edited_markdown)
    change = SectionChange(
        section=" · ".join(section.title for section in touched),
        start_line=first,
        end_line=last,
        before="\n".join(lines[first - 1:last]),
        after="\n".join(edited_lines[first - 1:last + delta]),
    )
    return (change,), tuple(section.title for section in sections if section not in touched)


def _has_content(lines: list[str], section: MarkdownSection) -> bool:
    return section.title != PREAMBLE_TITLE or any(
        line.strip() for line in lines[section.start_line - 1:section.end_line]
    )
