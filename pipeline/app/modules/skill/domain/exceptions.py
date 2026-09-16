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
