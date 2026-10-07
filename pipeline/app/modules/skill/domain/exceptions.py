class SkillNotFoundError(ValueError):
    code = "SKILL_NOT_FOUND"

    def __init__(self, skill_reference: str) -> None:
        super().__init__(f"Skill not found: {skill_reference}")


class SkillDisabledError(ValueError):
    code = "SKILL_DISABLED"

    def __init__(self, skill_id: str) -> None:
        super().__init__(f"Skill is disabled: {skill_id}")


class ReferenceDocumentTooLargeError(Exception):
    code = "REFERENCE_DOCUMENT_TOO_LARGE"

    def __init__(self) -> None:
        super().__init__("EDITABLE 참조 문서는 30,000자 이하여야 합니다.")


SKILL_REJECTION_CODES = frozenset(
    {
        "intent_ambiguous",
        "intent_unsupported",
        "invalid_instruction_length",
        "invalid_name",
        "invalid_reference",
        "skill_request_invalid",
    }
)


class SkillRequestRejectedError(ValueError):
    """Skill 작성 요청 거절. code(SKILL_REJECTION_CODES)로 거절 사유를 구분한다."""

    def __init__(self, code: str, message: str) -> None:
        if code not in SKILL_REJECTION_CODES:
            raise ValueError(f"Unknown Skill rejection code: {code}")
        super().__init__(message)
        self.code = code
