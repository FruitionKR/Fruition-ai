"""요약·번역 요청이 첨부 원문을 덮어쓰지 않게 하는 안전장치.

요약·번역은 원문과 따로 결과를 내는 말이라, 원문을 바꾸라는 말이 함께 있을 때만 첨부 수정으로 받는다.
라우터 프롬프트의 규칙을 코드로도 지키는 것이며, 모델이 잘못 골라도 승인 계획이 만들어지지 않게 한다.
어긋나면 라우터가 재시도해 답변·새 문서 중에서 다시 고른다.

전체 동사 허용 목록으로 막지 않는 이유: 수정 표현은 언어와 말투마다 끝이 없다. 요약·번역이 아닌 편집은 이 검사를 타지 않으므로
"더 부드러운 말투로"나 다른 언어의 요청도 라우터 판단대로 간다. 아래 목록에 없는 표현으로 요약·번역 결과를 반영해 달라고 하면
원문을 고치지 않고 답하는 쪽으로 틀리므로 문서가 망가지지 않는다.
"""

from __future__ import annotations

import re

SEPARATE_RESULT_GOALS = {"shorten", "translate"}
_SEPARATE_RESULT_WORDS = re.compile(r"요약|번역|summar|translat|tl;?dr", re.IGNORECASE)
# "고치지 말고"처럼 부정한 동사는 수정 요청이 아니다.
_NEGATED_VERB = re.compile(r"\S*지\s*(?:말|마|않)\S*|\b(?:do not|don't|dont|never|without)\s+\w+", re.IGNORECASE)
# 명사만으로는 세지 않는다. "수정 사항을 요약해 줘"는 수정 요청이 아니다.
_CHANGE_REQUEST = re.compile(
    r"고쳐|고치고|바꿔|바꾸어|바꾸고|덮어\s*[써쓰]|지워|지우고|덧붙|넣어|넣고|빼\s*줘|빼\s*주"
    r"|(?:수정|변경|추가|삭제|제거|교체|대체|반영|적용|교정|갱신|업데이트)\s*(?:해|하|시켜|부탁)"
    r"|(?:앞|끝|뒤|위|아래|밑)에\s*(?:적어|써|붙여|달아)"
    r"|\b(?:edit|fix|change|modify|update|rewrite|revise|replace|overwrite|correct|add|insert|append|remove|delete|apply)\b",
    re.IGNORECASE,
)
# 원문을 줄인다는 말이다. "줄여서 요약해 줘"처럼 요약과 함께 쓰이면 요약의 수식일 뿐이라 따로 본다.
_SHORTEN_REQUEST = re.compile(
    r"줄여|줄이고|다듬|(?:짧게|간결하게|간단하게)\s*(?:해|만들)"
    r"|\b(?:shorten|trim|condense)\b|\bmake\b.*\bshorter\b",
    re.IGNORECASE,
)


def lacks_attachment_change_request(message: str, edit_goal: str | None) -> bool:
    """요약·번역 요청인데 첨부 원문을 바꾸라는 말이 없으면 True다."""
    has_separate_result_word = _SEPARATE_RESULT_WORDS.search(message) is not None
    if edit_goal not in SEPARATE_RESULT_GOALS and not has_separate_result_word:
        return False
    stated = _NEGATED_VERB.sub(" ", message)
    if _CHANGE_REQUEST.search(stated):
        return False
    return has_separate_result_word or _SHORTEN_REQUEST.search(stated) is None
