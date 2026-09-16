import re


HEADING_PATTERN = re.compile(r"^ {0,3}#{1,6}\s+\S")
LIST_ITEM_PATTERN = re.compile(r"^(\s*)((?:[-+*]|\d+[.)]))\s+(?:(\[[ xX]\])\s+)?\S")
TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)*\s*:?-+:?\s*\|?\s*$")
FIXED_TEMPLATE_START = "# 고정 출력 템플릿\n\n```markdown\n"
FIXED_TEMPLATE_END = "\n```"


def _split_unescaped_pipes(line: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for index, character in enumerate(line):
        if character != "|":
            continue
        backslashes = 0
        for previous in reversed(line[start:index]):
            if previous != "\\":
                break
            backslashes += 1
        if backslashes % 2 == 0:
            parts.append(line[start:index])
            start = index + 1
    parts.append(line[start:])
    return parts


def _table_column_count(line: str) -> int:
    stripped = line.strip()
    parts = _split_unescaped_pipes(stripped)
    if stripped.startswith("|"):
        parts = parts[1:]
    if stripped.endswith("|") and not parts[-1]:
        parts = parts[:-1]
    return len(parts)


def _table_outer_pipes(line: str) -> tuple[bool, bool]:
    stripped = line.strip()
    return stripped.startswith("|"), bool(_split_unescaped_pipes(stripped)[-1] == "")


def _is_table_body_row(line: str, column_count: int, outer_pipes: tuple[bool, bool]) -> bool:
    stripped = line.strip()
    if not stripped or "|" not in stripped:
        return False
    if _table_outer_pipes(stripped) != outer_pipes:
        return False
    return _table_column_count(stripped) == column_count


def extract_markdown_structure(markdown: str) -> str:
    lines = markdown.splitlines()
    structure: list[str] = []
    fence_marker: str | None = None
    table_body_start: int | None = None
    table_columns: int | None = None
    table_outer_pipes: tuple[bool, bool] | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence_marker is None:
                table_body_start = None
                table_columns = None
                table_outer_pipes = None
                fence_marker = marker
            elif fence_marker == marker:
                fence_marker = None
            continue
        if fence_marker is not None:
            continue
        if table_body_start is not None:
            if index < table_body_start:
                continue
            if (
                table_columns is not None
                and table_outer_pipes is not None
                and _is_table_body_row(line, table_columns, table_outer_pipes)
            ):
                structure.append("| " + " | ".join([""] * table_columns) + " |")
                continue
            table_body_start = None
            table_columns = None
            table_outer_pipes = None
        if HEADING_PATTERN.match(line):
            structure.append(line.rstrip())
            continue
        list_item = LIST_ITEM_PATTERN.match(line)
        if list_item:
            indent, marker, checkbox = list_item.groups()
            prefix = f"{indent}{marker}"
            if checkbox:
                prefix += " [ ]"
            structure.append(f"{prefix} [item]")
            continue
        if (
            index + 1 < len(lines)
            and "|" in line
            and len(_split_unescaped_pipes(lines[index + 1])) > 1
            and TABLE_SEPARATOR_PATTERN.match(lines[index + 1])
        ):
            header_columns = _table_column_count(line)
            separator_columns = _table_column_count(lines[index + 1])
            if header_columns < 1 or header_columns != separator_columns:
                continue
            structure.extend((line.rstrip(), lines[index + 1].rstrip()))
            table_columns = separator_columns
            table_body_start = index + 2
            header = line.strip()
            table_outer_pipes = _table_outer_pipes(header)
    return "\n".join(structure)


def build_reference_template_instructions(template: str) -> str:
    return (
        "# 작성 규칙\n\n"
        "- 입력 내용을 아래 템플릿 구조에 맞춰 작성한다.\n"
        "- 제목, 섹션 순서, 목록과 표 구조를 변경하지 않는다.\n"
        "- 제공되지 않은 내용을 추측하지 않는다.\n\n"
        f"{FIXED_TEMPLATE_START}{template}{FIXED_TEMPLATE_END}"
    )


def extract_fixed_reference_template(instructions: str) -> str | None:
    start = instructions.find(FIXED_TEMPLATE_START)
    if start < 0:
        return None
    start += len(FIXED_TEMPLATE_START)
    end = instructions.find(FIXED_TEMPLATE_END, start)
    if end < 0:
        return None
    template = instructions[start:end]
    return template if template.strip() else None
