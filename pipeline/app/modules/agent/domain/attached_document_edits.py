"""첨부한 문서를 열지 않고 고치는 요청(열린 문서를 함께 고치는 경우 포함)에서 계획에 넣지 못한 문서와 그 안내 문구."""

from dataclasses import dataclass
from typing import Literal

UNTITLED_DOCUMENT = "제목 없는 문서"
OPEN_DOCUMENT_TITLE = "열린 문서"
ALL_FORBIDDEN_MESSAGE = (
    "편집 권한이 없어요. 편집할 수 있는 문서를 첨부해 다시 요청해 주세요. "
    "채팅에서 만든 문서나 변환 중인 문서도 수정할 수 없어요."
)
ExclusionReason = Literal["forbidden", "unsaved", "too_long", "empty", "unchanged", "failed"]
_EXCLUSION_SENTENCES: dict[ExclusionReason, str] = {
    "forbidden": "{titles}은(는) 편집 권한이 없어 제외했어요.",
    "unsaved": "{titles}은(는) 아직 저장되지 않아 수정하지 못했어요. 저장한 뒤 다시 요청해 주세요.",
    "too_long": "{titles}은(는) 너무 길어 수정하지 못했어요. 수정할 부분을 나눠 문서를 연 뒤 요청해 주세요.",
    "empty": "{titles}은(는) 내용이 없어 수정하지 못했어요.",
    "unchanged": "{titles}은(는) 바꿀 내용이 없어 계획에서 뺐어요.",
    "failed": "{titles}은(는) 수정안을 만들지 못했어요. 다시 요청해 주세요.",
}


@dataclass(frozen=True)
class ExcludedAttachment:
    title: str
    reason: ExclusionReason


def exclusion_notice(excluded: tuple[ExcludedAttachment, ...]) -> str | None:
    """제외한 문서를 이유별로 알리는 문장. 제외한 문서가 없으면 None이다."""
    sentences = []
    for reason, sentence in _EXCLUSION_SENTENCES.items():
        titles = [item.title or UNTITLED_DOCUMENT for item in excluded if item.reason == reason]
        if titles:
            sentences.append(sentence.format(titles=", ".join(f"\u2018{title}\u2019" for title in titles)))
    return " ".join(sentences) or None


def attachment_target_message(titles: tuple[str, ...], editable: tuple[bool, ...] = ()) -> str:
    """어느 첨부를 고칠지 되묻는다. 제목은 document-svc의 값을 정리한 것으로, 화면이 글자로만 보여 준다.

    읽기 전용 첨부는 후보가 아니어서 뺀다. 번호는 라우터가 보는 첨부 순번이라 그대로 둔다.
    """
    numbered = tuple(enumerate(titles, start=1))
    candidates = tuple(item for item, can_edit in zip(numbered, editable) if can_edit) or numbered
    listed = "\n".join(f"{index}. {title or UNTITLED_DOCUMENT}" for index, title in candidates)
    return f"첨부한 문서 중 어느 문서를 고칠까요? 번호나 제목으로 알려 주세요.\n{listed}"
