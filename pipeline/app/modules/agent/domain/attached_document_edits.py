"""첨부한 문서를 열지 않고 고치는 요청에서 계획에 넣지 못한 문서와 그 안내 문구."""

from dataclasses import dataclass
from typing import Literal

from app.core.response_language import is_english, localized, register_en

UNTITLED_DOCUMENT = "제목 없는 문서"
ALL_FORBIDDEN_MESSAGE = register_en(
    "편집 권한이 없어요. 편집할 수 있는 문서를 첨부해 다시 요청해 주세요. "
    "채팅에서 만든 문서나 변환 중인 문서도 수정할 수 없어요.",
    "You don't have edit permission. Attach documents you can edit and try again. "
    "Documents created in chat or still being converted can't be edited either.",
)
ExclusionReason = Literal["forbidden", "too_long", "empty", "unchanged", "failed"]
_EXCLUSION_SENTENCES: dict[ExclusionReason, str] = {
    "forbidden": "{titles}은(는) 편집 권한이 없어 제외했어요.",
    "too_long": "{titles}은(는) 너무 길어 수정하지 못했어요. 수정할 부분을 나눠 문서를 연 뒤 요청해 주세요.",
    "empty": "{titles}은(는) 내용이 없어 수정하지 못했어요.",
    "unchanged": "{titles}은(는) 바꿀 내용이 없어 계획에서 뺐어요.",
    "failed": "{titles}은(는) 수정안을 만들지 못했어요. 다시 요청해 주세요.",
}
_EXCLUSION_SENTENCES_EN: dict[ExclusionReason, str] = {
    "forbidden": "Excluded {titles} because you don't have edit permission.",
    "too_long": "Couldn't edit {titles} because it's too long. Open the document and ask for smaller parts.",
    "empty": "Couldn't edit {titles} because it has no content.",
    "unchanged": "Left {titles} out of the plan because there was nothing to change.",
    "failed": "Couldn't draft an edit for {titles}. Please try again.",
}


@dataclass(frozen=True)
class ExcludedAttachment:
    title: str
    reason: ExclusionReason


def exclusion_notice(excluded: tuple[ExcludedAttachment, ...]) -> str | None:
    """제외한 문서를 이유별로 알리는 문장. 제외한 문서가 없으면 None이다."""
    sentences = []
    templates = _EXCLUSION_SENTENCES_EN if is_english() else _EXCLUSION_SENTENCES
    for reason, sentence in templates.items():
        titles = [item.title or _untitled() for item in excluded if item.reason == reason]
        if titles:
            sentences.append(sentence.format(titles=", ".join(f"\u2018{title}\u2019" for title in titles)))
    return " ".join(sentences) or None


def attachment_target_message(titles: tuple[str, ...]) -> str:
    """어느 첨부를 고칠지 되묻는다. 제목은 document-svc의 값을 정리한 것으로, 화면이 글자로만 보여 준다."""
    listed = "\n".join(f"{index}. {title or _untitled()}" for index, title in enumerate(titles, start=1))
    question = localized(
        "첨부한 문서 중 어느 문서를 고칠까요? 번호나 제목으로 알려 주세요.",
        "Which attached document should I edit? Tell me its number or title.",
    )
    return f"{question}\n{listed}"


def _untitled() -> str:
    return localized(UNTITLED_DOCUMENT, "Untitled document")
