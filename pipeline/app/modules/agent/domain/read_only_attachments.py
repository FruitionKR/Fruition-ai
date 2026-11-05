"""읽기 전용 첨부는 근거로만 쓴다. 프론트가 보기 권한으로 연 문서를 첨부 맨 앞에 실어, 고칠 문서를 고를 때 후보에서 뺀다."""

from dataclasses import replace

from app.modules.agent.domain.attached_edit_request import (
    lacks_attachment_change_request,
    names_open_document,
    points_at_one_document,
    selects_attachment,
)
from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute

_ATTACHED_EDIT_PLACEMENTS = {("replace", "target"), ("insert_after", "document_end")}


def route_without_read_only_targets(route: AgentTurnRoute, request: AgentTurnRequest) -> AgentTurnRoute:
    """어떤 라우터가 골랐든 읽기 전용 첨부를 고칠 문서로 추측하지 않는다.

    문장이 그 문서를 가리켰거나 다른 후보가 없으면 그대로 두어 기존 권한 안내로 끝난다.
    모델이 편집 가능한 첨부까지 함께 골랐으면 그대로 두어, 고치고 나서 뺀 문서를 알린다.
    """
    flags = request.reference_document_editable
    read_only = tuple(number for number, editable in enumerate(flags, start=1) if not editable)
    if not read_only:
        return route
    editable = tuple(number for number, can_edit in enumerate(flags, start=1) if can_edit)
    targets = route.attached_edit_targets
    if not targets and not _asks_for_an_unchosen_edit(route, request):
        return route
    if _means_open_read_only_document(request, editable):
        return _edit_route(route, read_only)
    if set(targets) & set(editable):
        return route
    if not editable:
        return route if targets else _edit_route(route, read_only)
    named = tuple(number for number in read_only if _title(request, number) and _title(request, number) in request.message)
    if named:
        return route if targets else _edit_route(route, named)
    if points_at_one_document(request.message):
        return route
    if len(editable) == 1:
        return _edit_route(route, editable)
    return _ask_which_attachment(route)


def _asks_for_an_unchosen_edit(route: AgentTurnRoute, request: AgentTurnRequest) -> bool:
    """고칠 문서를 고르지 못한 수정 요청이다. 열린 문서가 있으면 그 문서가 대상이므로 빼고, 첨부를 되묻는 route만 남긴다."""
    if route.action == "clarify" and route.notice_kind == "attachment_target":
        return not lacks_attachment_change_request(request.message, None)
    return (
        route.action in {"markdown_edit", "workspace_workflow"}
        and route.document_operation == "edit"
        and not _has_open_markdown(request)
    )


def _means_open_read_only_document(request: AgentTurnRequest, editable: tuple[int, ...]) -> bool:
    """편집 문맥 없이 "열린 문서"라고 했으면 보기 권한으로 연 문서뿐이다. 다른 첨부를 고치면 말하지 않은 문서가 바뀐다."""
    return (
        not _has_open_markdown(request)
        and names_open_document(request.message)
        and not selects_attachment(request.message)
        and not any(_title(request, number) and _title(request, number) in request.message for number in editable)
    )


def _title(request: AgentTurnRequest, number: int) -> str:
    titles = request.reference_document_titles
    return titles[number - 1] if number <= len(titles) else ""


def _has_open_markdown(request: AgentTurnRequest) -> bool:
    context = request.active_markdown_context
    return context is not None and bool(context.markdown.strip())


def _edit_route(route: AgentTurnRoute, targets: tuple[int, ...]) -> AgentTurnRoute:
    was_edit = route.document_operation == "edit"
    placement = (route.edit_operation, route.edit_destination)
    keeps_placement = placement in _ATTACHED_EDIT_PLACEMENTS
    return replace(
        route,
        action="workspace_workflow",
        retrieval_source=route.retrieval_source if was_edit else "none",
        document_operation="edit",
        persist=True,
        required_capabilities=route.required_capabilities if was_edit else ("document-edit",),
        edit_goal="other" if route.edit_goal in {None, "create_from_chat"} else route.edit_goal,
        edit_operation=route.edit_operation if keeps_placement else "replace",
        edit_destination=route.edit_destination if keeps_placement else "target",
        skill_candidates=(),
        notice_kind=None,
        attached_edit_targets=targets,
        include_active_markdown=False,
    )


def _ask_which_attachment(route: AgentTurnRoute) -> AgentTurnRoute:
    return replace(
        route,
        action="clarify",
        retrieval_source="none",
        document_operation="none",
        persist=False,
        required_capabilities=(),
        edit_goal=None,
        edit_operation=None,
        edit_destination=None,
        selected_skill_id=None,
        skill_candidates=(),
        notice_kind="attachment_target",
        attached_edit_targets=(),
        include_active_markdown=False,
    )
