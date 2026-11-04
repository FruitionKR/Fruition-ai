"""첨부한 문서를 열지 않고 고치는 요청의 안내 문구."""

UNTITLED_DOCUMENT = "제목 없는 문서"


def attachment_target_message(titles: tuple[str, ...]) -> str:
    """어느 첨부를 고칠지 되묻는다. 제목은 document-svc의 값을 정리한 것으로, 화면이 글자로만 보여 준다."""
    listed = "\n".join(f"{index}. {title or UNTITLED_DOCUMENT}" for index, title in enumerate(titles, start=1))
    return f"첨부한 문서 중 어느 문서를 고칠까요? 번호나 제목으로 알려 주세요.\n{listed}"
