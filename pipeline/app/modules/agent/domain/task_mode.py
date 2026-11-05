"""작업 탭(mode=task) 턴의 route 규칙."""

from dataclasses import replace

from app.modules.agent.domain.attached_edit_request import (
    lacks_attachment_change_request,
    lacks_save_or_change_request,
    saves_result_as_new_document,
    saves_result_into_document,
    uses_only_open_document,
)
from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute

# 첨부 없는 작업 탭에서는 답변 갈래를 고를 수 없다. 질문은 clarify로 채팅 탭을 안내한다.
TASK_MODE_EXCLUDED_ACTIONS = frozenset({"chat_answer", "conversation_reply"})


def is_task_mode(request: AgentTurnRequest) -> bool:
    return request.mode == "task"


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
    """첨부 UI는 작업 탭에만 있으므로, 첨부가 있는 턴은 답변 갈래로 첨부를 근거로 답한다.

    열린 문서의 요약·번역도 답변 갈래다. 채팅 탭은 위키 질의만 보내 열린 문서로 답할 수 없다.
    """
    return (
        not is_task_mode(request)
        or bool(request.reference_document_ids)
        or summarizes_open_document(request)
        or action not in TASK_MODE_EXCLUDED_ACTIONS
    )


def task_mode_route(route: AgentTurnRoute, request: AgentTurnRequest) -> AgentTurnRoute:
    """작업 탭 규칙을 적용한다. 어떤 라우터가 골랐든 결과가 같도록 애플리케이션에서 정한다.

    - 첨부 없음: 답변 갈래는 다시 묻지 않고 채팅 탭 안내용 clarify로 바꾼다. 질문을 작업으로 억지로
      바꾸면 의도하지 않은 편집·저장이 생기므로, 안내만 하고 끝낸다.
    - 첨부 있음: 채팅 탭 안내 clarify는 질문이라는 판단이므로 첨부를 근거로 답하는 chat_answer로 바꾼다.
      첨부는 작업 탭에서만 고를 수 있어 채팅 탭 안내로는 답을 얻을 수 없다.
    - 첨부 없이 열린 문서의 요약·번역만 시킴: 첨부가 있을 때와 같게 열린 문서를 근거로 답한다.
    """
    if not is_task_mode(request):
        return route
    if request.reference_document_ids or summarizes_open_document(request):
        return _attachment_answer_route(route) if _is_chat_tab_clarify(route) else route
    if route.action not in TASK_MODE_EXCLUDED_ACTIONS:
        return route
    return replace(
        route,
        action="clarify",
        reason=route.reason or "question in task mode",
        edit_goal=None,
        edit_operation=None,
        edit_destination=None,
        selected_skill_id=None,
        skill_candidates=(),
        retrieval_source="none",
        document_operation="none",
        persist=False,
        required_capabilities=(),
        notice_kind="chat_tab",
    )


def _is_chat_tab_clarify(route: AgentTurnRoute) -> bool:
    # 작업 탭에서 안내 종류가 없는 문서 무관 clarify도 채팅 탭 문구로 끝난다.
    return (
        route.action == "clarify"
        and route.document_operation == "none"
        and route.edit_goal is None
        and not route.skill_candidates
        and route.notice_kind in {"chat_tab", None}
    )


def _attachment_answer_route(route: AgentTurnRoute) -> AgentTurnRoute:
    return replace(route, action="chat_answer", retrieval_source="workspace", notice_kind=None)
