import logging

from app.core.response_language import localize_text, register_en
from app.modules.query.application.ports import QueryEventPublisherPort

logger = logging.getLogger(__name__)

# 진행 이벤트의 고정 문구. 화면은 stage로 갈래를 정하고 message는 그대로 보여 준다.
# 영어 요청이면 publish_query_event가 이 목록으로 바꾼다. 목록에 없는 문구는 한국어로 남는다.
for _ko, _en in (
    ("요청과 사용할 수 있는 스킬을 확인하고 있어요.", "Checking your request and the available skills."),
    ("처리 결과와 대화 내용을 정리하고 있어요.", "Wrapping up the result and the conversation."),
    ("문서 편집 요청으로 확인했어요.", "Identified this as a document edit request."),
    ("새 문서 작성 요청으로 확인했어요.", "Identified this as a new document request."),
    ("질문에 답할 근거를 찾고 있어요.", "Looking for evidence to answer your question."),
    ("대화 맥락에 맞는 답변을 작성하고 있어요.", "Writing a reply that fits the conversation."),
    ("폴더 정리 계획을 준비하고 있어요.", "Preparing a folder organization plan."),
    ("폴더 내용을 확인하고 있어요.", "Checking the folder contents."),
    ("문서 작업 계획을 준비하고 있어요.", "Preparing a document task plan."),
    ("스킬 작성 요청을 처리하고 있어요.", "Handling your skill authoring request."),
    ("완료한 작업을 바탕으로 스킬 제안을 준비하고 있어요.", "Preparing a skill proposal from your completed tasks."),
    ("추가로 확인할 내용을 정리하고 있어요.", "Putting together what I need to confirm."),
    ("요청의 지원 범위를 확인하고 있어요.", "Checking whether the request is supported."),
    ("고칠 문서를 읽고 있어요.", "Reading the documents to edit."),
    ("고른 폴더의 문서를 읽고 있어요.", "Reading the documents in the selected folder."),
    ("첨부한 문서를 읽고 있어요.", "Reading the attached documents."),
    ("검사에서 보완할 부분을 발견해 다시 작성하고 있어요.", "The check found something to improve, so I'm rewriting it."),
    ("편집안이 요청에 맞는지 확인하고 있어요.", "Checking that the edit matches your request."),
    ("새 문서가 Skill 지침에 맞는지 확인하고 있어요.", "Checking that the new document follows the skill instructions."),
    ("검토 결과를 반영해 답변을 다시 작성하고 있어요.", "Rewriting the answer based on the review."),
    ("답변이 질문과 근거에 맞는지 검토하고 있어요.", "Reviewing whether the answer fits the question and evidence."),
    ("답변 생성을 완료했습니다.", "Finished generating the answer."),
    ("질의 처리를 시작했습니다.", "Started processing your question."),
):
    register_en(_ko, _en)


def publish_query_event(
    event_publisher: QueryEventPublisherPort | None,
    stage: str,
    message: str,
    data: dict[str, object] | None = None,
) -> None:
    if event_publisher is None:
        return
    try:
        event_publisher.publish(stage, localize_text(message) or message, data)
    except Exception as exc:
        # 진행 이벤트는 유실을 허용하지만, 조용히 사라지면 발행이 계속 실패해도 알 수 없다.
        logger.warning("[질의 진행 이벤트 발행 실패] stage=%s errorType=%s", stage, type(exc).__name__)
