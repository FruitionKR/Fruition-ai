from dataclasses import dataclass

from app.modules.query.domain.exceptions import InvalidQuestionError


@dataclass(frozen=True)
class Question:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise InvalidQuestionError("질문은 비어 있을 수 없습니다.")

    @property
    def normalized(self) -> str:
        return " ".join(self.value.strip().split())

