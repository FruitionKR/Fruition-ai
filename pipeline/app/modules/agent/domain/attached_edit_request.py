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


# 저장하라는 말이다. 첨부 없이 열린 문서만 있는 턴의 "요약해서 저장해 줘"는 원문을 두고 새 문서로 저장한다.
_SAVE_REQUEST = re.compile(r"저장|\bsave\b", re.IGNORECASE)
# 저장할 곳으로 열린 문서를 가리킨 표현이다("이 문서에 요약해서 저장해 줘"). 가리키는 말이 없으면("문서에 저장해 줘", "별도 문서에 저장해 줘") 열린 문서를
# 지목한 게 아니므로 라우터 프롬프트의 "저장할 곳을 말하지 않은 저장"과 같게 새 문서로 만든다. 놓쳐도 원문을 고치지 않는 쪽으로 틀린다.
# 사이의 말이 수식하는 어미로 끝나거나("이 문서에 있는 내용을"), 내용을 가리키거나("이 문서에 대해"), 다른 곳을 말하면("이 문서에 요약해서 따로") 저장할 곳이 아니다.
_POINTS_AT_OPEN_DOCUMENT = (
    r"(?:(?:(?<![가-힣])이|현재|지금|열린|열려\s*있는|열어\s*둔)\s*(?:문서|노트)|원문|원본|여기)"
)
_SAVE_INTO_DOCUMENT = re.compile(
    rf"{_POINTS_AT_OPEN_DOCUMENT}(?:\s*(?:맨\s*)?(?:앞|끝|뒤|위|아래|밑))?에\s+"
    r"(?:(?!대해|대하여|관해|관하여|따라|맞춰|맞게|새|별도|별개|따로|다른|신규)\S*[^\s는은던한된둔본준쓴힌온긴간에]\s+){0,3}저장"
)
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


# 번역을 시키는 말이다. 뒤에 다른 명사가 붙거나("번역 용어집") 수식하는 어미로 끝나면("번역하는 방법", "번역된 부분") 시키는 말이 아니다.
_TRANSLATION_REQUEST = re.compile(
    r"번역\s*(?:본|문서|결과|버전)?\s*(?:을|를|으로|로)?\s*"
    r"(?:해(?!\s*(?:둔|놓은|놨|뒀))|하(?:고|여|자|세|십|시[오겠죠]|라(?!는|고))|좀|부탁|시켜|돌려|만들|저장|작성|보여|줘|주세|(?:한\s*)?(?:후|뒤|다음)"
    r"|(?:이?요)?\s*(?:[.!?,~…]|$))"
    r"|\btranslate\b|\btranslation\s*(?:please|of\b|[.!?]*\s*$)",
    re.IGNORECASE | re.MULTILINE,
)
_SUMMARY_WORD = re.compile(r"요약|summar|tl;?dr", re.IGNORECASE)
# 뜻이 여럿인 말("일어난", "불어난", "독어")은 넣지 않는다. 그런 언어는 아래 조사 붙은 꼴로 센다.
_LANGUAGE_NAME = (
    r"(?:영어|영문|한국어|한글|일본어|중국어|프랑스어|독일어|스페인어|포르투갈어|이탈리아어|러시아어|베트남어|태국어|아랍어|힌디어"
    r"|인도네시아어|네덜란드어|튀르키예어|터키어|폴란드어|우크라이나어|라틴어"
    r"|(?<![a-z])(?:english|korean|japanese|chinese|french|german|spanish|portuguese|italian|russian|vietnamese|thai|arabic"
    r"|hindi|indonesian|dutch|turkish|polish|ukrainian|latin)(?![a-z]))"
)
# 번역할 언어를 말한 표현이다. 언어 이름 뒤에 무엇이 와도 세되("영어 버전으로", "영어판으로", "English로"), 이름이 번역할 원본을 가리키면("영어 문서를 번역해 줘") 세지 않는다.
# 목록에 없는 언어는 꼴로 본다("스와힐리어로", "일어 번역", "영문으로", "into Swahili"). 놓치면 언어를 한 번 더 묻는 쪽으로 틀린다.
# "논문 번역"처럼 문서를 가리키는 말을 세지 않으려고 "문"은 "으로"가 붙을 때만 센다.
_STATES_LANGUAGE = re.compile(
    rf"{_LANGUAGE_NAME}(?!\s*(?:문서|논문|파일|자료|원문|원본|본문|글|메일|기사|책)(?!으?로))"
    r"|\S+(?:어|말|글)\s*(?:로|으로|번역)|\S+문\s*으로|\b(?:to|into|in)\s+[a-z]+",
    re.IGNORECASE,
)
# 언어를 되물은 뒤의 답이다. 언어와 그 말을 맺는 어미뿐이어야 한다. "영어 공부법 알려 줘"나 "됐어"는 답이 아니라 새 요청이다.
_LANGUAGE_ANSWER = re.compile(
    rf"\s*(?:(?:네|응|음|그럼)[,\s]+)?(?:(?:into|to|in)\s+)?"
    rf"(?P<language>{_LANGUAGE_NAME}(?:\s*(?:버전|판|문))?(?:으로|로)?|[가-힣]+(?:어|말|글)(?:으로|로)|[가-힣]+문으로)"
    r"(?:\s*(?:번역|바꿔|옮겨|저장|해|줘|주세요|좀|부탁|드려요|드립니다|합니다|할게요|요|이요|입니다|please|translate|[.!~,]))*\s*",
    re.IGNORECASE,
)


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


def saves_result_into_document(message: str) -> bool:
    """요약·번역 결과를 열린 문서에 저장하라는 말이면 True다."""
    return _SAVE_INTO_DOCUMENT.search(_NEGATED_VERB.sub(" ", message)) is not None


def saves_result_as_new_document(message: str) -> bool:
    """요약·번역 결과를 저장하라면서 원문을 바꾸거나 원문에 저장하라는 말이 없으면 True다."""
    return (
        lacks_attachment_change_request(message, None)
        and not lacks_save_or_change_request(message, None)
        and not saves_result_into_document(message)
    )


def needs_translation_language(message: str, source_markdown: str) -> bool:
    """번역하라면서 언어를 말하지 않았는데 원문이 한국어면 True다. 기본 언어가 한국어라 그대로 두면 원문과 같은 글이 나온다.

    한글이 한 글자라도 있으면 한국어 문서로 보는 것은 질의 답변의 언어 판단(_fallback_language)과 같다.
    한국어가 아닌 문서는 기본 언어인 한국어로 옮기면 되므로 묻지 않는다. 요약이 함께 있으면 원문과 같은 글이 되지 않아 묻지 않는다.
    """
    stated = _NEGATED_VERB.sub(" ", _SECTION_NAMED_AFTER_RESULT.sub(" ", message))
    return (
        _TRANSLATION_REQUEST.search(stated) is not None
        and _SUMMARY_WORD.search(stated) is None
        and _STATES_LANGUAGE.search(stated) is None
        and any("가" <= character <= "힣" for character in source_markdown)
    )


def answer_to_translation_language_question(answer: str, asked_request: str) -> str | None:
    """언어를 되물은 뒤의 답이면 앞선 요청에 그 언어를 붙여 돌려준다. 답이 아니면 None이다.

    "영어로"만 보면 저장하라는 말도 번역하라는 말도 없어 어떤 검사도 타지 않는다. 앞선 요청으로 되돌려야 같은 안전장치를 탄다.
    "영어로 바꿔 줘"도 되물은 언어의 답이라 원문을 고치는 말로 받지 않는다. 열린 문서를 가리키거나 저장을 다시 말했으면 그 말이 온전한 새 요청이다.
    """
    matched = _LANGUAGE_ANSWER.fullmatch(answer)
    if matched is None:
        return None
    return f"{matched.group('language')} {asked_request}"


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
