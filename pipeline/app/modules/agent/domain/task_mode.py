"""탭별 route 규칙. 작업 탭(mode=task)은 문서를 고치고 만들고 정리하며, 채팅 탭(mode=chat)은 문서를 근거로 답한다."""

from dataclasses import replace

from app.modules.agent.domain.attached_edit_request import (
    lacks_attachment_change_request,
    lacks_save_or_change_request,
    saves_result_as_new_document,
    saves_result_into_document,
    uses_only_open_document,
)
from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute, NoticeKind

# 작업 탭에서는 답변 갈래를 고를 수 없다. 질문은 첨부가 있어도 clarify로 채팅 탭을 안내한다.
TASK_MODE_EXCLUDED_ACTIONS = frozenset({"chat_answer", "conversation_reply"})
# 채팅 탭에서 실행할 수 있는 갈래. 나머지는 원문이나 워크스페이스를 바꾸므로 작업 탭을 안내한다.
CHAT_MODE_ACTIONS = frozenset({"chat_answer", "conversation_reply", "reject"})


def is_task_mode(request: AgentTurnRequest) -> bool:
    return request.mode == "task"


def is_chat_mode(request: AgentTurnRequest) -> bool:
    return request.mode == "chat"


def summarizes_open_document(request: AgentTurnRequest) -> bool:
    """첨부 없이 열린 문서의 요약·번역만 시켰으면 True다. 바꾸거나 저장하라는 말이 없으므로 원문을 두고 답한다.

    라우터가 고른 edit_goal이 아니라 문장만 본다. 어떤 라우터가 골랐든 수정 차단과 답변 허용, 답변 근거가 같은 판단을 쓴다.
    """
    return _only_open_document(request) and lacks_save_or_change_request(request.message, None)


def saves_open_document_result_as_new(request: AgentTurnRequest) -> bool:
    """첨부 없이 열린 문서의 요약·번역을 저장하라면 원문을 두고 새 문서로 만든다."""
    return _only_open_document(request) and saves_result_as_new_document(request.message)


def saves_open_document_result_into_it(request: AgentTurnRequest) -> bool:
    """첨부 없이 열린 문서의 요약·번역을 그 문서에 저장하라는 요청이다."""
    return (
        _only_open_document(request)
        and lacks_attachment_change_request(request.message, None)
        and saves_result_into_document(request.message)
    )


def creates_result_only_from_open_document(request: AgentTurnRequest) -> bool:
    """문서를 연 첨부 턴에 "이 문서"의 요약·번역을 새 문서로 만드는 요청이다. 첨부를 함께 주면 다른 문서의 내용이 섞인다."""
    context = request.active_markdown_context
    return (
        bool(request.reference_document_ids)
        and context is not None
        and bool(context.markdown.strip())
        and uses_only_open_document(request.message, request.reference_document_titles)
        and lacks_attachment_change_request(request.message, None)
    )


def _only_open_document(request: AgentTurnRequest) -> bool:
    context = request.active_markdown_context
    return not request.reference_document_ids and context is not None and bool(context.markdown.strip())


def task_mode_allows_action(request: AgentTurnRequest, action: str) -> bool:
    return not is_task_mode(request) or action not in TASK_MODE_EXCLUDED_ACTIONS


def task_mode_route(route: AgentTurnRoute, request: AgentTurnRequest) -> AgentTurnRoute:
    """탭 규칙을 적용한다. 어떤 라우터가 골랐든 결과가 같도록 애플리케이션에서 정한다.

    - 작업 탭: 답변 갈래는 첨부와 열린 문서가 있어도 채팅 탭 안내용 clarify로 바꾼다. 질문을 작업으로 억지로
      바꾸면 의도하지 않은 편집·저장이 생기므로, 안내만 하고 끝낸다.
    - 채팅 탭: 답변과 거절만 실행한다. 나머지는 실행하지 않고 작업 탭을 안내한다.
    """
    if is_chat_mode(request):
        return _chat_mode_route(route)
    if not is_task_mode(request) or route.action not in TASK_MODE_EXCLUDED_ACTIONS:
        return route
    return _notice_route(route, "chat_tab", route.reason or "question in task mode")


def _chat_mode_route(route: AgentTurnRoute) -> AgentTurnRoute:
    if route.action in CHAT_MODE_ACTIONS:
        return route
    if _is_notice_clarify(route) and route.notice_kind == "web_disabled":
        return route
    # 채팅 탭 안내나 자세히 묻는 clarify는 이미 채팅 탭에 있으므로 질문으로 받아 답한다.
    if _is_notice_clarify(route) and route.notice_kind in {"chat_tab", "details", None}:
        return replace(route, action="chat_answer", retrieval_source="workspace", notice_kind=None)
    return _notice_route(route, "task_tab", route.reason or "change request in chat mode")


def _notice_route(route: AgentTurnRoute, notice_kind: NoticeKind, reason: str) -> AgentTurnRoute:
    return replace(
        route,
        action="clarify",
        reason=reason,
        edit_goal=None,
        edit_operation=None,
        edit_destination=None,
        selected_skill_id=None,
        skill_candidates=(),
        retrieval_source="none",
        document_operation="none",
        persist=False,
        required_capabilities=(),
        attached_edit_targets=(),
        include_active_markdown=False,
        notice_kind=notice_kind,
    )


def _is_notice_clarify(route: AgentTurnRoute) -> bool:
    return (
        route.action == "clarify"
        and route.document_operation == "none"
        and route.edit_goal is None
        and not route.skill_candidates
    )
