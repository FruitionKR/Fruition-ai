from __future__ import annotations

import re

from app.modules.wiki_schema.application.schema_sections import SCHEMA_SECTIONS
from app.modules.wiki_schema.domain.entities import SchemaFilterResult, SchemaFragments, SchemaIssue


SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)(sk-[a-z0-9_-]{8,})"),
    re.compile(r"(?i)(api[_ -]?key|token|password|secret|private[_ -]?key)\s*[:=]\s*([^\s`]+)"),
)


def build_schema_preview(result: SchemaFilterResult) -> str:
    sections = ["# 적용될 Schema 설정"]
    applied_markdown = _render_applied_sections(result.fragments)
    if applied_markdown:
        sections.append(applied_markdown)
    else:
        sections.append("적용될 설정이 없습니다.")

    blocked = [issue for issue in result.issues if issue.severity == "blocked"]
    if blocked:
        sections.append(_render_issues("적용되지 않은 설정", blocked))

    unclear = [issue for issue in result.issues if issue.severity == "unclear"]
    if unclear:
        sections.append(_render_issues("확인 필요한 설정", unclear))

    return "\n\n".join(sections).strip()


def _render_applied_sections(fragments: SchemaFragments) -> str:
    rendered_sections: list[str] = []
    for field_name, title in SCHEMA_SECTIONS:
        markdown = getattr(fragments, field_name).strip()
        if markdown:
            rendered_sections.append(f"## {title}\n{_redact_secret_values(markdown)}")
    return "\n\n".join(rendered_sections)


def _render_issues(title: str, issues: list[SchemaIssue]) -> str:
    lines = [f"## {title}"]
    for issue in issues:
        lines.append(f"- {_redact_secret_values(issue.text)}")
        lines.append(f"  - 사유: {issue.reason}")
    return "\n".join(lines)


def _redact_secret_values(text: str) -> str:
    redacted = SECRET_VALUE_PATTERNS[0].sub("[REDACTED_SECRET]", text)
    return SECRET_VALUE_PATTERNS[1].sub(lambda match: f"{match.group(1)}=[REDACTED_SECRET]", redacted)
