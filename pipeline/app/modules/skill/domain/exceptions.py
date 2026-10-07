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


class SkillRequestRejectedError(ValueError):
    """사용자가 고칠 수 있는 Skill 작성 요청 거절. code로 거절 사유를 구분한다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
