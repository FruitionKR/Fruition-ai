from __future__ import annotations

import re
from collections.abc import Iterable
from typing import cast

from app.modules.wiki_schema.application.schema_sections import SCHEMA_SECTION_NAMES
from app.modules.wiki_schema.domain.entities import (
    SchemaFilterResult,
    SchemaFragments,
    SchemaIssue,
    SchemaIssueCategory,
)


BLOCKING_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        "상위 지시를 무시하라는 요청입니다.",
        re.compile(
            r"(system|developer|instruction|지시|규칙|정책).{0,20}(무시|ignore|따르지|forget|잊어)",
            re.IGNORECASE,
        ),
    ),
    (
        "hidden_prompt",
        "숨겨진 prompt 또는 내부 정책 공개 요청입니다.",
        re.compile(
            r"(system prompt|hidden prompt|developer message|숨겨진\s*prompt|시스템\s*프롬프트|내부\s*정책).{0,20}(보여|출력|공개|알려|show|print|reveal)",
            re.IGNORECASE,
        ),
    ),
    (
        "policy_weakening",
        "근거 또는 불확실성 정책을 약화하는 요청입니다.",
        re.compile(
            r"(출처\s*없이|근거\s*없이|citation.{0,12}(none|disable|없이)|모르면\s*지어|지어내|hallucinate|단정)",
            re.IGNORECASE,
        ),
    ),
    (
        "permission_escalation",
        "사용자 승인, 파일 접근, 외부 요청 권한을 확대하는 요청입니다.",
        re.compile(
            r"(승인\s*없이|묻지\s*말고|자동으로).{0,30}(수정|삭제|파일|요청|접근|실행)|"
            r"(로컬\s*파일|모든\s*파일|\.env).{0,30}(읽|접근|사용)|"
            r"(외부\s*url|외부\s*요청).{0,30}(자동|보내|호출)",
            re.IGNORECASE,
        ),
    ),
    (
        "secret",
        "민감정보를 저장하거나 출력하려는 요청입니다.",
        re.compile(
            r"(api[_ -]?key|token|password|private[_ -]?key|secret|credential|\.env|비밀번호|토큰|개인키|시크릿)",
            re.IGNORECASE,
        ),
    ),
    (
        "role_override",
        "모델 역할이나 정책 우선순위를 바꾸려는 요청입니다.",
        re.compile(
            r"(너는|you are).{0,30}(보안\s*검사\s*하지|정책을\s*무시|system보다|developer보다|우선)",
            re.IGNORECASE,
        ),
    ),
)


def filter_schema_fragments(raw_markdown: str, fragments: SchemaFragments) -> SchemaFilterResult:
    issues = _find_issues("raw_markdown", raw_markdown)
    cleaned_values: dict[str, str] = {}

    for section_name in SCHEMA_SECTION_NAMES:
        section_text = getattr(fragments, section_name)
        cleaned_text, section_issues = _clean_fragment(section_name, section_text)
        cleaned_values[section_name] = cleaned_text
        issues.extend(section_issues)

    cleaned_values["concept_markdown"] = _ensure_concept_evidence_guard(cleaned_values["concept_markdown"])

    return SchemaFilterResult(
        fragments=SchemaFragments(**cleaned_values),
        issues=issues,
    )


def _clean_fragment(section_name: str, markdown: str) -> tuple[str, list[SchemaIssue]]:
    kept_lines: list[str] = []
    issues: list[SchemaIssue] = []

    for line in markdown.splitlines():
        line_issues = _find_issues(section_name, line)
        if line_issues:
            issues.extend(line_issues)
            continue
        line = _normalize_concept_mandatory_language(section_name, line)
        kept_lines.append(line)

    return _trim_blank_lines(kept_lines), issues


def _find_issues(section_name: str, text: str) -> list[SchemaIssue]:
    if not text.strip():
        return []

    issues: list[SchemaIssue] = []
    for category, reason, pattern in BLOCKING_RULES:
        for match in pattern.finditer(text):
            issues.append(
                SchemaIssue(
                    severity="blocked",
                    category=cast(SchemaIssueCategory, category),
                    text=match.group(0).strip(),
                    reason=reason,
                    section=section_name,
                )
            )
    return issues


def _normalize_concept_mandatory_language(section_name: str, line: str) -> str:
    if "concept" not in line.lower():
        return line
    if not any(marker in line for marker in ("꼭", "반드시", "무조건", "항상")):
        return line

    content = re.sub(r"^\s*[-*]\s*", "", line).strip()
    content = re.sub(r"(은|는)?\s*concept.*", "", content, flags=re.IGNORECASE).strip()
    content = content.rstrip(".。")
    if not content:
        content = "사용자가 요청한 concept"
    return f"- {content}은 문서 근거가 있을 때 concept 후보로 우선 검토한다."


def _ensure_concept_evidence_guard(markdown: str) -> str:
    if not markdown.strip():
        return ""
    if "문서 근거" in markdown or "문서에 근거" in markdown:
        return markdown
    return f"{markdown}\n- 위 항목은 문서 근거가 있을 때 concept 후보로 우선 검토한다."


def _trim_blank_lines(lines: Iterable[str]) -> str:
    values = list(lines)
    while values and not values[0].strip():
        values.pop(0)
    while values and not values[-1].strip():
        values.pop()
    return "\n".join(values)
