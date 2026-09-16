import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SkillSafetyIssue:
    category: str
    text: str
    reason: str
    severity: str = "blocked"
    start: int | None = None
    end: int | None = None
    source_type: str = "instruction"
    reference_document_id: str | None = None


BLOCKED_INSTRUCTION_MARKERS = {
    "approval_bypass": ("승인 없이", "승인을 생략", "bypass approval", "without approval"),
    "permission_escalation": ("권한을 무시", "권한 우회", "ignore permission", "bypass permission"),
    "forbidden_tool": ("shell 실행", "sql 실행", "run shell", "execute sql"),
    "policy_weakening": (
        "시스템 정책을 무시",
        "이전 지시를 무시",
        "ignore system policy",
        "ignore previous instructions",
        "forget previous instructions",
    ),
    "hidden_prompt": (
        "시스템 프롬프트를 보여",
        "시스템 프롬프트를 출력",
        "reveal system prompt",
        "show system prompt",
    ),
    "role_override": ("act as system", "developer message로 행동", "시스템 역할로 행동"),
}
CREDENTIAL_PATTERNS = (
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?"
        r"(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\Z)",
        re.IGNORECASE,
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|api[_ -]?token|"
        r"private[_ -]?token|client[_ -]?secret|secret|password)"
        r"\s*[:=]\s*['\"]?[^\s'\"]{8,}",
        re.IGNORECASE,
    ),
)
EMAIL_PATTERN = re.compile(
    r"(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}"
    r"(?=$|[^A-Z0-9.-]|\.+(?=[^A-Z0-9.-]|$))",
    re.IGNORECASE,
)
PHONE_PATTERNS = (
    re.compile(r"(?<!\d)(?:\+82[-. ]?)?0?1[016789][-. ]?\d{3,4}[-. ]?\d{4}(?!\d)"),
    re.compile(r"(?<!\d)0(?:2|[3-6]\d|70|80)[-. ]?\d{3,4}[-. ]?\d{4}(?!\d)"),
    re.compile(r"(?<!\d)\+\d(?:[-. ]?\d){7,14}(?!\d)"),
)
RESIDENT_REGISTRATION_PATTERN = re.compile(r"(?<!\d)\d{6}[- ]?[1-4]\d{6}(?!\d)")
PAYMENT_CARD_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
BANK_ACCOUNT_PATTERN = re.compile(
    r"(?:계좌(?:번호)?|account(?:\s+number)?)\s*[:#-]?\s*"
    r"(?P<value>\d(?:[- ]?\d){8,15})(?!\d)",
    re.IGNORECASE,
)
PERSONAL_FIELD_CATEGORIES = {
    "이름": "personal_name",
    "성명": "personal_name",
    "실명": "personal_name",
    "person name": "personal_name",
    "full name": "personal_name",
    "주소": "personal_address",
    "home address": "personal_address",
    "postal address": "personal_address",
    "이메일": "personal_email",
    "email": "personal_email",
    "e-mail": "personal_email",
    "전화번호": "personal_phone",
    "연락처": "personal_phone",
    "phone": "personal_phone",
    "주민등록번호": "resident_registration_number",
    "주민번호": "resident_registration_number",
    "카드번호": "payment_card",
    "card number": "payment_card",
    "계좌번호": "bank_account",
    "account number": "bank_account",
}
PERSONAL_FIELD_PATTERN = re.compile(
    r"^[ \t]*(?:>[ \t]*)*(?:(?:[-+*]|\d+[.)])\s+)?(?P<label>"
    + "|".join(
        re.escape(label)
        for label in sorted(PERSONAL_FIELD_CATEGORIES, key=len, reverse=True)
    )
    + r")[ \t]*[:：][ \t]*(?P<value>[^\r\n]*)\r?$",
    re.IGNORECASE | re.MULTILINE,
)
TABLE_SEPARATOR_PATTERN = re.compile(
    r"^\s*\|?(?:\s*:?-+:?\s*\|)*\s*:?-+:?\s*\|?\s*$"
)

PERSONAL_DATA_REASONS = {
    "personal_email": "Skill에는 실제 이메일 주소를 포함할 수 없습니다.",
    "personal_phone": "Skill에는 실제 전화번호를 포함할 수 없습니다.",
    "resident_registration_number": "Skill에는 주민등록번호를 포함할 수 없습니다.",
    "payment_card": "Skill에는 결제 카드번호를 포함할 수 없습니다.",
    "bank_account": "Skill에는 계좌번호를 포함할 수 없습니다.",
    "personal_name": "Skill에는 실제 사람 이름이나 이름 placeholder를 포함할 수 없습니다.",
    "personal_address": "Skill에는 실제 주소나 주소 placeholder를 포함할 수 없습니다.",
}


def inspect_skill_instructions(instructions_markdown: str) -> tuple[SkillSafetyIssue, ...]:
    lowered = instructions_markdown.lower()
    issues: list[SkillSafetyIssue] = []
    for category, markers in BLOCKED_INSTRUCTION_MARKERS.items():
        for marker in markers:
            start = 0
            while (start := lowered.find(marker, start)) >= 0:
                issues.append(
                    SkillSafetyIssue(
                        category=category,
                        text=marker,
                        reason="Skill은 시스템 권한·승인·tool 정책을 변경할 수 없습니다.",
                        start=start,
                        end=start + len(marker),
                    )
                )
                start += len(marker)
    issues.extend(_personal_data_issues(instructions_markdown))
    for pattern in CREDENTIAL_PATTERNS:
        issues.extend(
            SkillSafetyIssue(
                category="credential",
                text="[credential]",
                reason="Skill에는 인증정보를 포함할 수 없습니다.",
                start=match.start(),
                end=match.end(),
            )
            for match in pattern.finditer(instructions_markdown)
        )
    return _deduplicate_issues(issues)


def _personal_data_issues(value: str) -> list[SkillSafetyIssue]:
    issues = [
        _personal_data_issue("personal_email", match.start(), match.end())
        for match in EMAIL_PATTERN.finditer(value)
    ]
    for pattern in PHONE_PATTERNS:
        issues.extend(
            _personal_data_issue("personal_phone", match.start(), match.end())
            for match in pattern.finditer(value)
        )
    issues.extend(
        _personal_data_issue("resident_registration_number", match.start(), match.end())
        for match in RESIDENT_REGISTRATION_PATTERN.finditer(value)
        if _is_valid_resident_registration_number(match.group())
    )
    issues.extend(
        _personal_data_issue("payment_card", match.start(), match.end())
        for match in PAYMENT_CARD_PATTERN.finditer(value)
        if _is_valid_payment_card(match.group())
    )
    for match in BANK_ACCOUNT_PATTERN.finditer(value):
        issues.append(
            _personal_data_issue(
                "bank_account",
                match.start("value"),
                match.end("value"),
            )
        )
    issues.extend(_personal_field_issues(value))
    issues.extend(_personal_table_issues(value))
    return issues


def _personal_field_issues(value: str) -> list[SkillSafetyIssue]:
    issues: list[SkillSafetyIssue] = []
    for match in PERSONAL_FIELD_PATTERN.finditer(value):
        field_value = match.group("value")
        if _is_blank_template_value(field_value):
            continue
        category = PERSONAL_FIELD_CATEGORIES[match.group("label").lower()]
        leading_spaces = len(field_value) - len(field_value.lstrip())
        trailing_spaces = len(field_value) - len(field_value.rstrip())
        start = match.start("value") + leading_spaces
        end = match.end("value") - trailing_spaces
        issues.append(_personal_data_issue(category, start, end))
    return issues


def _personal_table_issues(value: str) -> list[SkillSafetyIssue]:
    lines = value.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    issues: list[SkillSafetyIssue] = []
    for index in range(len(lines) - 1):
        header = lines[index].rstrip("\r\n")
        separator = lines[index + 1].rstrip("\r\n")
        if "|" not in header or not TABLE_SEPARATOR_PATTERN.fullmatch(separator):
            continue
        sensitive_columns = {
            column: PERSONAL_FIELD_CATEGORIES[cell.lower()]
            for column, (cell, _, _) in enumerate(_table_cells(header, offsets[index]))
            if cell.lower() in PERSONAL_FIELD_CATEGORIES
        }
        if not sensitive_columns:
            continue
        for row_index in range(index + 2, len(lines)):
            row = lines[row_index].rstrip("\r\n")
            if not row.strip() or "|" not in row:
                break
            cells = _table_cells(row, offsets[row_index])
            for column, category in sensitive_columns.items():
                if column >= len(cells):
                    continue
                cell, start, end = cells[column]
                if not _is_blank_template_value(cell):
                    issues.append(_personal_data_issue(category, start, end))
    return issues


def _table_cells(line: str, offset: int) -> list[tuple[str, int, int]]:
    parts = line.split("|")
    cells: list[tuple[str, int, int]] = []
    cursor = 0
    for index, part in enumerate(parts):
        part_start = cursor
        cursor += len(part) + 1
        if index == 0 and line.startswith("|"):
            continue
        if index == len(parts) - 1 and line.endswith("|"):
            continue
        stripped = part.strip()
        leading_spaces = len(part) - len(part.lstrip())
        cells.append(
            (
                stripped,
                offset + part_start + leading_spaces,
                offset + part_start + leading_spaces + len(stripped),
            )
        )
    return cells


def _personal_data_issue(category: str, start: int, end: int) -> SkillSafetyIssue:
    return SkillSafetyIssue(
        category=category,
        text=f"[{category}]",
        reason=PERSONAL_DATA_REASONS[category],
        start=start,
        end=end,
    )


def _is_blank_template_value(value: str) -> bool:
    stripped = value.strip()
    return not stripped or not stripped.replace("_", "").strip()


def _is_valid_resident_registration_number(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) != 13 or digits[6] not in "1234":
        return False
    century = "19" if digits[6] in "12" else "20"
    try:
        datetime.strptime(century + digits[:6], "%Y%m%d")
    except ValueError:
        return False
    weighted_sum = sum(
        int(digit) * weight
        for digit, weight in zip(digits[:12], (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5), strict=True)
    )
    return (11 - weighted_sum % 11) % 10 == int(digits[-1])


def _is_valid_payment_card(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        number = int(digit)
        if index % 2 == parity:
            number *= 2
            if number > 9:
                number -= 9
        checksum += number
    return checksum % 10 == 0


def _deduplicate_issues(issues: list[SkillSafetyIssue]) -> tuple[SkillSafetyIssue, ...]:
    ordered = sorted(
        issues,
        key=lambda issue: (
            issue.start if issue.start is not None else -1,
            -((issue.end or 0) - (issue.start or 0)),
        ),
    )
    kept: list[SkillSafetyIssue] = []
    for issue in ordered:
        if issue.start is None or issue.end is None:
            kept.append(issue)
            continue
        if any(
            existing.start is not None
            and existing.end is not None
            and issue.start < existing.end
            and existing.start < issue.end
            for existing in kept
        ):
            continue
        kept.append(issue)
    return tuple(kept)
