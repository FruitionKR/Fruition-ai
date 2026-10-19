"""Skill 지침의 실행 시점 자리표시자(`{문서}`, `{선택한 부분}`, `{요청}`)를 채운다.

- `{요청}`: 사용자의 요청 문장.
- `{선택한 부분}`: 선택한 본문. 선택이 없으면 `(선택 없음)`.
- `{문서}`: 편집 모델이 이미 받는 본문(편집 payload의 markdown, 테스트 실행의 reference_context)이 있으면
  본문을 다시 싣지 않고 그 위치를 가리키는 짧은 표시로 바꾼다. 본문이 payload에 없을 때만 길이를 잘라 넣는다.

이 세 가지만 바꾸고, 그 밖의 중괄호(`{이름}`, `{{...}}` 등)는 그대로 둔다. 채운 값 안에 다시 자리표시자가
있어도 한 번에 치환하므로 다시 바뀌지 않는다.
"""

import re

DOCUMENT_PLACEHOLDER = "{문서}"
SELECTION_PLACEHOLDER = "{선택한 부분}"
REQUEST_PLACEHOLDER = "{요청}"
NO_SELECTION = "(선택 없음)"
NO_DOCUMENT = "(현재 문서 없음)"
EDIT_PAYLOAD_DOCUMENT_REFERENCE = "[현재 문서: 요청 payload의 markdown 본문]"
REFERENCE_CONTEXT_DOCUMENT_REFERENCE = "[현재 문서: 요청 payload의 reference_context.source_document 본문]"
MAX_INLINE_DOCUMENT_CHARS = 8_000
MAX_INLINE_SELECTION_CHARS = 4_000
MAX_INLINE_REQUEST_CHARS = 2_000
TRUNCATED_SUFFIX = "\n…(이하 생략)"

_PLACEHOLDER_PATTERN = re.compile(
    "|".join(re.escape(token) for token in (DOCUMENT_PLACEHOLDER, SELECTION_PLACEHOLDER, REQUEST_PLACEHOLDER))
)


def render_skill_instructions(
    instructions: str,
    *,
    request: str,
    selection: str | None = None,
    document: str | None = None,
    document_reference: str | None = None,
) -> str:
    """지침의 자리표시자를 채운다. document_reference가 있으면 `{문서}`는 본문 대신 그 표시가 된다."""
    if document_reference is not None:
        document_value = document_reference
    elif document is not None and document.strip():
        document_value = _clip(document, MAX_INLINE_DOCUMENT_CHARS)
    else:
        document_value = NO_DOCUMENT
    selection_value = _clip(selection, MAX_INLINE_SELECTION_CHARS) if selection and selection.strip() else NO_SELECTION
    values = {
        DOCUMENT_PLACEHOLDER: document_value,
        SELECTION_PLACEHOLDER: selection_value,
        REQUEST_PLACEHOLDER: _clip(request.strip(), MAX_INLINE_REQUEST_CHARS),
    }
    return _PLACEHOLDER_PATTERN.sub(lambda match: values[match.group(0)], instructions)


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + TRUNCATED_SUFFIX
