"""요약·번역 요청이 원문(첨부, 열린 문서)을 덮어쓰지 않게 하는 안전장치.

요약·번역은 원문과 따로 결과를 내는 말이라, 원문을 바꾸라는 말이 함께 있을 때만 수정으로 받는다.
라우터 프롬프트의 규칙을 코드로도 지키는 것이며, 모델이 잘못 골라도 수정안이 만들어지지 않게 한다.
어긋나면 라우터가 재시도해 답변·새 문서 중에서 다시 고른다.

전체 동사 허용 목록으로 막지 않는 이유: 수정 표현은 언어와 말투마다 끝이 없다. 요약·번역이 아닌 편집은 이 검사를 타지 않으므로
"더 부드러운 말투로"나 다른 언어의 요청도 라우터 판단대로 간다. 아래 목록에 없는 표현으로 요약·번역 결과를 반영해 달라고 하면
원문을 고치지 않고 답하는 쪽으로 틀리므로 문서가 망가지지 않는다.

요약·번역이라는 말이 있으면 그 말의 앞뒤를 나눠 본다. 뒤에 수정 표현이 있으면 마지막으로 시킨 일이 수정이다("요약해서 고쳐 줘").
앞의 수정 표현은 대부분 수식어라("수정하는 방법을 요약해 줘", "고쳐 둔 부분을 요약해 줘") 연결·명령 어미로 끝날 때만 센다("오타를 고치고 요약해 줘").
수식하는 어미는 끝이 없지만 연결·명령 어미는 몇 개 안 되고, 빠뜨려도 원문을 고치지 않는 쪽으로 틀린다.

"요약 섹션을 보강해 줘"의 "요약 섹션"은 고칠 대상을 가리키는 말이라 결과물 요구로 세지 않는다. 그 밖의 요약·번역이라는 말은
결과물 요구로 본다("요약 좀", "요약본"). 영어 수정 동사는 "Summarize the latest update"처럼 명사로도 쓰여 명령형 자리에서만 센다.
"""

from __future__ import annotations

import re

SEPARATE_RESULT_GOALS = {"shorten", "translate"}
_SEPARATE_RESULT_WORDS = re.compile(r"요약|번역|summar|translat|tl;?dr", re.IGNORECASE)
# 문서의 일부를 가리키는 이름이다. "Summarize section 3"은 동사라 해당하지 않는다.
_SECTION_NAMED_AFTER_RESULT = re.compile(
    r"(?:요약|번역)\s*(?:섹션|부분|단락|문단|항목|파트|챕터)"
    r"|\b(?:summary|translation)\s+(?:section|part|paragraph|chapter|heading)\b",
    re.IGNORECASE,
)
# "고치지 말고"처럼 부정한 동사는 수정 요청이 아니다.
_NEGATED_VERB = re.compile(r"\S*지\s*(?:말|마|않)\S*|\b(?:do not|don't|dont|never|without)\s+\w+", re.IGNORECASE)
_ENGLISH_CHANGE_VERB = r"(?:edit|fix|change|modify|update|rewrite|revise|replace|overwrite|correct|add|insert|append|remove|delete|apply)"
# "문서에 적어 둔 내용", "위에 써 있는 메모"는 이미 있는 글을 가리키므로 시키는 말이 뒤따를 때만 센다.
_WRITE_REQUEST_ENDING = (
    r"(?:(?:요|라)?(?:[.!?,~…]|$)"
    r"|\s*(?:줘|줄래|줄\s*수|줬으면|주(?:세|셔|시[오겠면죠길고]|실|십|면|고|길|라|렴)"
    r"|둬|둘래|두(?:어|고|세|십|시[오겠죠])"
    r"|놔|놓(?:아|고|을래|으(?:세|십|시[오겠죠]))"
    r"|봐|보(?:세|자)|달라(?!는)|야\s*(?:해|돼)|서\s*(?:줘|주세)|(?:서\s*)?저장|넣기))"
)
# 위치 바로 뒤나, 목적어를 사이에 두고 쓰라는 말이다("문서에 요약을 적어 줘"). 사이의 말이 수식하거나 잇는 어미로 끝나면 위치가 쓸 곳이 아니다("문서에 있는 요약을 적어 줘").
_PLAIN_WORD = r"\S*[^\s는은던한된둔본준서고]\s+"
_WRITE_CLAUSE = rf"(?:앞|끝|뒤|위|아래|밑|문서)에(?:\s*|\s+(?:{_PLAIN_WORD}){{1,3}})(?:적어|써|붙여|달아)"
_CHANGE_NOUN = r"(?:수정|변경|추가|삭제|제거|교체|대체|반영|적용|교정|갱신|업데이트)"
# 영어는 명령형 자리만 센다. 문장 첫머리·요청하는 말 뒤는 동사만으로, "and"·쉼표 뒤는 명사 나열일 수 있어 목적어가 이어질 때만 센다.
_ENGLISH_CHANGE_REQUEST = (
    r"(?:^\s*|[.;!?]\s+|\b(?:please|then|also|just|let's|(?:can|could|would|will)\s+you|(?:want|need|like)\s+you\s+to)\s+)"
    rf"{_ENGLISH_CHANGE_VERB}\b(?!-)"
    rf"|(?:\band\s+|,\s*){_ENGLISH_CHANGE_VERB}\b(?!-)"
    r"(?=\s+(?:it|this|that|them|these|those|the|a|an|my|our|to|in|into|on|at)\b|\s*[.!]?\s*$)"
)
# 명사만으로는 세지 않는다. "수정 사항을 요약해 줘"는 수정 요청이 아니다.
_CHANGE_REQUEST = re.compile(
    r"고쳐|고치고|바꿔|바꾸어|바꾸고|덮어\s*[써쓰]|지워|지우고|덧붙|넣어|넣고|빼\s*줘|빼\s*주"
    rf"|{_CHANGE_NOUN}\s*(?:해|하|시켜|부탁)"
    rf"|{_WRITE_CLAUSE}{_WRITE_REQUEST_ENDING}"
    rf"|{_ENGLISH_CHANGE_REQUEST}",
    re.IGNORECASE | re.MULTILINE,
)
# 요약·번역이라는 말 앞의 수정 표현이다. 어미 없이 끝나거나("오타를 고쳐 요약해 줘") 잇거나 시키는 어미일 때만 센다.
# "적어 두고 안 본 메모", "변경해 달라고 한 내용", "수정하고 싶은 부분", "수정하라는 의견"은 뒤의 말을 꾸미므로 세지 않는다.
# "고쳐 두고 요약해 줘", "고치고 나서 세 줄로 요약해 줘"처럼 요약이 바로(방식을 말하는 부사구만 두고) 이어지면 차례로 시키는 말이다.
_UP_TO_RESULT_WORD = rf"\s*(?:(?:{_PLAIN_WORD})?\S*[로게]\s+)?$"
_CHANGE_CLAUSE_BEFORE_RESULT = re.compile(
    rf"(?:{_WRITE_CLAUSE}|고쳐|바꿔|바꾸어|지워|넣어|덮어\s*써|덧붙여|빼|{_CHANGE_NOUN}\s*(?:해|시켜))"
    rf"(?!\s*(?:두고|놓고|달라고)(?!{_UP_TO_RESULT_WORD}))(?:서|{_UP_TO_RESULT_WORD}|{_WRITE_REQUEST_ENDING})"
    rf"|(?:고치고|바꾸고|지우고|넣고|덮어\s*쓰고|덧붙이고"
    rf"|{_CHANGE_NOUN}\s*(?:부탁|하(?:고|여|세|십|시[오겠죠]|라(?!는|고)|자(?!는|고))))(?!\s*(?:싶|있|계|나(?!서{_UP_TO_RESULT_WORD})))"
    rf"|{_CHANGE_NOUN}\s*하면서{_UP_TO_RESULT_WORD}"
    rf"|{_ENGLISH_CHANGE_REQUEST}",
    re.IGNORECASE | re.MULTILINE,
)
_KOREAN_RESULT_WORD = re.compile(r"요약|번역")
_WRITE_REQUEST = re.compile(rf"{_WRITE_CLAUSE}{_WRITE_REQUEST_ENDING}", re.MULTILINE)
# 원문을 줄인다는 말이다. "줄여서 요약해 줘"처럼 요약과 함께 쓰이면 요약의 수식일 뿐이라 따로 본다.
_SHORTEN_REQUEST = re.compile(
    r"줄여|줄이고|다듬|(?:짧게|간결하게|간단하게)\s*(?:해|만들)"
    r"|\b(?:shorten|trim|condense)\b|\bmake\b.*\bshorter\b",
    re.IGNORECASE,
)


# 저장하라는 말이다. 첨부 없이 열린 문서만 있는 턴의 "요약해서 저장해 줘"는 그 문서를 바꾸는 요청으로 계속 받는다.
_SAVE_REQUEST = re.compile(r"저장|\bsave\b", re.IGNORECASE)
# 열린 문서를 가리키는 말이다. "이 문서"는 첨부를 가리킬 때가 많아 세지 않는다.
_NAMES_OPEN_DOCUMENT = re.compile(
    r"(?:열린|열려\s*있는|열어\s*둔|현재|지금)\s*(?:문서|노트)|\b(?:open|opened|current)\s+(?:document|doc|note)\b",
    re.IGNORECASE,
)
_NAMES_ATTACHMENT = re.compile(r"첨부|\battach", re.IGNORECASE)
# 첨부를 고칠 대상이 아니라 근거·제외로만 말한 표현이다. "첨부 내용대로", "첨부한 문서를 참고해서", "첨부 문서 말고"
_ATTACHMENT_AS_SOURCE = re.compile(
    r"첨부\S*?(?:\s+(?:내용|문서|파일|자료|본문)\S*?)?\s*(?:대로|처럼|기준|바탕|참고|참조|반영|따라|맞춰|맞게|말고)\S*"
    r"|\b(?:based\s+on|according\s+to|using|from)\s+(?:the\s+)?attach\w*",
    re.IGNORECASE,
)
# 첨부를 순번·전체로 고르는 말이다.
_SELECTS_ATTACHMENTS = re.compile(r"둘\s*다|모두|전부|각각|\d+\s*개|번째|\b(?:both|all|each)\b", re.IGNORECASE)
# 열린 문서를 지목해도 문서 자체를 순번으로 고르면 첨부를 고르는 말이다. "현재 문서 형식에 맞춰 두 번째 문서를 고쳐 줘"
_SELECTS_DOCUMENT_BY_ORDER = re.compile(r"번째\s*(?:문서|파일)")


def _asks_change(stated: str) -> bool:
    """요약·번역이라는 말이 있으면 그 뒤는 모든 수정 표현을, 앞은 잇거나 시키는 어미로 끝난 수정 표현만 센다."""
    without_sections = _SECTION_NAMED_AFTER_RESULT.sub(" ", stated)
    last_result_word = None
    for last_result_word in _KOREAN_RESULT_WORD.finditer(without_sections):
        pass
    if last_result_word is None:
        return _CHANGE_REQUEST.search(stated) is not None
    # "문서에 요약을 적어 줘"는 요약이라는 말이 쓸 곳과 쓰라는 말 사이에 있어 나누기 전에 본다.
    return (
        any(write.end() > last_result_word.start() for write in _WRITE_REQUEST.finditer(without_sections))
        or _CHANGE_REQUEST.search(without_sections[last_result_word.end() :]) is not None
        or _CHANGE_CLAUSE_BEFORE_RESULT.search(without_sections[: last_result_word.start()]) is not None
    )


def lacks_attachment_change_request(message: str, edit_goal: str | None) -> bool:
    """요약·번역 요청인데 원문을 바꾸라는 말이 없으면 True다."""
    asks_separate_result = _SEPARATE_RESULT_WORDS.search(_SECTION_NAMED_AFTER_RESULT.sub(" ", message)) is not None
    if edit_goal not in SEPARATE_RESULT_GOALS and not asks_separate_result:
        return False
    # 지운 자리를 표시해 둔다. 공백만 남기면 "고쳐 주지 말고 요약해 줘"가 "고쳐 요약해 줘"로 읽힌다.
    stated = _NEGATED_VERB.sub(" ∅ ", message)
    if _asks_change(stated):
        return False
    return asks_separate_result or _SHORTEN_REQUEST.search(stated) is None


def lacks_save_or_change_request(message: str, edit_goal: str | None) -> bool:
    """요약·번역 요청인데 원문을 바꾸거나 저장하라는 말이 모두 없으면 True다."""
    stated = _NEGATED_VERB.sub(" ", message)
    return lacks_attachment_change_request(message, edit_goal) and _SAVE_REQUEST.search(stated) is None


def names_open_document(message: str) -> bool:
    return _NAMES_OPEN_DOCUMENT.search(message) is not None


def names_attachment(message: str) -> bool:
    return _NAMES_ATTACHMENT.search(message) is not None


def selects_attachment(message: str) -> bool:
    """첨부를 말하거나 순번·전체로 고르면 True다.

    열린 문서를 지목한 문장에서 순번·전체 표현은 그 문서 안의 항목을 세고("열린 문서의 오타를 모두 고쳐 줘"),
    근거로만 말한 첨부는 고칠 대상이 아니다("첨부 내용대로 열린 문서를 고쳐 줘").
    """
    if _SELECTS_DOCUMENT_BY_ORDER.search(message):
        return True
    if names_open_document(message):
        return names_attachment(_ATTACHMENT_AS_SOURCE.sub(" ", message))
    return names_attachment(message) or _SELECTS_ATTACHMENTS.search(message) is not None


def attached_edit_instruction(message: str, *, position: int, total: int) -> str:
    """여러 첨부를 한 문서씩 고칠 때의 지시. 한 문서만 받는 편집·평가가 "모두"를 다른 문서까지 요구하지 않게 한다.

    문서 제목은 신뢰하지 않는 값이라 넣지 않는다. 사용자의 요청 문장은 그대로 두고 범위만 덧붙이므로 평가 기준은 그대로다.
    """
    if total <= 1:
        return message
    return (
        f"{message}\n\n"
        f"[적용 범위] 이 요청은 첨부 문서 {total}개에 문서별로 따로 적용된다. 지금은 {total}개 중 {position}번째 문서만 다룬다. "
        "이 문서 안에서 요청을 빠짐없이 수행했는지만 판단하고, 다른 문서의 변경은 요구하거나 언급하지 않는다."
    )
