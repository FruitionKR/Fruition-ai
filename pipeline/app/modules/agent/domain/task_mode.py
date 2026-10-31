"""작업 탭(mode=task) 턴의 route 규칙."""

from dataclasses import replace

from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute

# 작업 탭에서는 답변 갈래를 고를 수 없다. 질문은 clarify로 채팅 탭을 안내한다.
TASK_MODE_EXCLUDED_ACTIONS = frozenset({"chat_answer", "conversation_reply"})


def is_task_mode(request: AgentTurnRequest) -> bool:
    return request.mode == "task"


def task_mode_route(route: AgentTurnRoute, request: AgentTurnRequest) -> AgentTurnRoute:
    """작업 탭에서 답변 갈래가 나오면 다시 묻지 않고 채팅 탭 안내용 clarify로 바꾼다.

    질문을 작업으로 억지로 바꾸면 의도하지 않은 편집·저장이 생기므로, 안내만 하고 끝낸다.
    """
    if not is_task_mode(request) or route.action not in TASK_MODE_EXCLUDED_ACTIONS:
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
