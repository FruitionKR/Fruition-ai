import re


def markdown_section(markdown: str, heading: str) -> str:
    lines = markdown_section_lines(markdown, heading)
    return "\n".join(line.strip() for line in lines if line.strip()).strip()


def markdown_list_section(markdown: str, heading: str) -> list[str]:
    items = []
    for line in markdown_section_lines(markdown, heading):
        stripped = line.strip()
        if not stripped or stripped == "-":
            continue
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        if stripped and not stripped.startswith("-"):
            items.append(stripped)
    return items


def markdown_section_lines(markdown: str, heading: str) -> list[str]:
    lines = []
    in_section = False
    heading_pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.IGNORECASE)
    for line in markdown.splitlines():
        if heading_pattern.match(line.strip()):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            lines.append(line)
    return lines
