"""작업 탭(mode=task) 턴의 route 규칙."""

from dataclasses import replace

from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute

# 첨부 없는 작업 탭의 일반 질문은 채팅 탭으로 안내한다. Fruition 사용법(product_help)은 여기서 답한다.
TASK_MODE_EXCLUDED_ACTIONS = frozenset({"chat_answer", "conversation_reply"})


def is_task_mode(request: AgentTurnRequest) -> bool:
    return request.mode == "task"


def task_mode_allows_action(request: AgentTurnRequest, action: str) -> bool:
    """첨부 UI는 작업 탭에만 있으므로, 첨부가 있는 턴은 답변 갈래로 첨부를 근거로 답한다."""
    return (
        not is_task_mode(request)
        or bool(request.reference_document_ids)
        or action not in TASK_MODE_EXCLUDED_ACTIONS
    )


def task_mode_route(route: AgentTurnRoute, request: AgentTurnRequest) -> AgentTurnRoute:
    """작업 탭 규칙을 적용한다. 어떤 라우터가 골랐든 결과가 같도록 애플리케이션에서 정한다.

    - 첨부 없음: 답변 갈래는 다시 묻지 않고 채팅 탭 안내용 clarify로 바꾼다. 질문을 작업으로 억지로
      바꾸면 의도하지 않은 편집·저장이 생기므로, 안내만 하고 끝낸다.
    - 첨부 있음: 채팅 탭 안내 clarify는 질문이라는 판단이므로 첨부를 근거로 답하는 chat_answer로 바꾼다.
      첨부는 작업 탭에서만 고를 수 있어 채팅 탭 안내로는 답을 얻을 수 없다.
    """
    if not is_task_mode(request):
        return route
    if request.reference_document_ids:
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
