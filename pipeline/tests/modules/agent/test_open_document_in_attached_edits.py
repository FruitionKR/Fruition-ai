"""열린 문서와 첨부를 함께 고치라는 요청. 열린 문서가 계획에서 조용히 빠지면 안 된다."""

import json
from dataclasses import replace

import pytest

from app.modules.agent.domain.attached_edit_request import asks_open_document_with_attachments
from app.modules.agent.domain.entities import ActiveMarkdownContext, AgentTurnRequest, AgentTurnResult
from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    ChatCompletionsTurnRouter,
    _normalize_route,
    _repaired_route,
    _route_failures,
    route_payload,
)
from app.modules.agent.interfaces.http.responses import _to_response
from tests.modules.agent.test_attached_document_edits import (
    Editor,
    Reader,
    _document,
    _route,
    _use_case,
)
from tests.modules.agent.test_attached_edit_routing import SequenceClient, _raw

OPEN = ActiveMarkdownContext(markdown="# 열린 문서\n매출 본문")
BUG_MESSAGE = "3개 모두 '매출'을 '영업이익'으로 수정해 줘"


def _turn(message: str, *attachments: str, opened: ActiveMarkdownContext | None = OPEN, **values: object) -> AgentTurnRequest:
    return AgentTurnRequest(
        **{
            "message": message,
            "workspace_id": "workspace-1",
            "user_id": "user-1",
            "active_markdown_context": opened,
            "document_id": "doc-1" if opened else None,
            "base_version": 3 if opened else None,
            "reference_document_ids": attachments or ("doc-2", "doc-3"),
            **values,
        }
    )


def _failures(raw: dict[str, object], request: AgentTurnRequest) -> list[str]:
    route, failures = _normalize_route(raw)
    return [*failures, *_route_failures(_repaired_route(route, request), request)]


def _names_flag(failures: list[str]) -> bool:
    return any("include_active_markdown" in failure for failure in failures)


def _both() -> dict[str, object]:
    return _raw(attached_edit_targets=[1, 2], include_active_markdown=True, edit_goal="other")


def _attachments_only() -> dict[str, object]:
    return _raw(attached_edit_targets=[1, 2], edit_goal="other")


# 라우터 계약


def test_route_defaults_to_not_including_open_document() -> None:
    route, failures = _normalize_route(_attachments_only())

    assert failures == []
    assert route.include_active_markdown is False


@pytest.mark.parametrize("value", ["true", 1, None])
def test_invalid_flag_shape_is_contract_failure(value: object) -> None:
    _, failures = _normalize_route(_raw(include_active_markdown=value))

    assert _names_flag(failures)


@pytest.mark.parametrize(
    "message",
    [
        BUG_MESSAGE,
        "세 개 모두 고쳐 줘",
        "3개 문서를 모두 고쳐 줘",
        "문서 3개 전부 고쳐 줘",
        "이 3개를 고쳐 줘",
        "셋 다 고쳐 줘",
        "세 문서 다 고쳐 줘",
        "열린 문서와 첨부 두 개 모두 고쳐 줘",
        "열린 문서랑 첨부한 문서를 고쳐 줘",
        "현재 문서와 첨부 문서 둘 다 고쳐 줘",
        "첨부 문서와 열린 문서를 모두 고쳐 줘",
        "첨부한 두 문서랑 지금 문서 다 고쳐 줘",
        "이 문서와 첨부 문서 모두 고쳐 줘",
        "첨부 문서를 고치고 열린 문서도 고쳐 줘",
        "Fix the open document and the attachments",
        "Fix the attachments and the current document",
        "All three need the same fix, please change them",
        # 숫자 없이 전부를 고르면 열린 문서도 포함한다.
        "'매출'을 모두 '영업이익'으로 바꿔 줘",
        "문서 전부 고쳐 줘",
        "이 문서들 모두 고쳐 줘",
        "각각 고쳐 줘",
        "다 고쳐 줘",
        "Fix all of them",
    ],
)
def test_attachment_only_route_retries_when_message_includes_open_document(message: str) -> None:
    request = _turn(message)

    assert _names_flag(_failures(_attachments_only(), request))
    assert _failures(_both(), request) == []


@pytest.mark.parametrize(
    "message",
    [
        # 첨부만 지목했다.
        "첨부한 문서 3개 모두 '매출'을 '영업이익'으로 수정해 줘",
        "첨부한 문서 2개 모두 고쳐 줘",
        "첨부 문서 둘 다 고쳐 줘",
        "첨부 문서 모두 고쳐 줘",
        "Fix both attachments",
        # 지목 수가 첨부 수와 같으면 첨부다.
        "두 문서 모두 고쳐 줘",
        "둘 다 고쳐 줘",
        "2개 모두 고쳐 줘",
        # 열린 문서는 근거이거나 제외했다.
        "열린 문서 형식에 맞춰 첨부 문서 둘 다 고쳐 줘",
        "현재 문서와 비교해서 첨부 문서 둘 다 고쳐 줘",
        "열린 문서 말고 첨부 두 개 모두 고쳐 줘",
        # 제목이나 순번으로 첨부를 골랐다.
        "회의록과 기획서 모두 고쳐 줘",
        "첫 번째와 두 번째 문서 모두 고쳐 줘",
    ],
)
def test_attachment_only_requests_stay_as_they_are(message: str) -> None:
    request = _turn(message, reference_document_titles=("회의록", "기획서"))

    assert not _names_flag(_failures(_attachments_only(), request))


@pytest.mark.parametrize(
    "message",
    ["첨부한 문서 3개 모두 고쳐 줘", "첨부 문서 둘 다 고쳐 줘", "Fix both attachments"],
)
def test_flag_is_rejected_when_only_attachments_were_named(message: str) -> None:
    assert _names_flag(_failures(_both(), _turn(message)))


@pytest.mark.parametrize(
    "message",
    [
        # 문서 안의 항목을 세는 말이다.
        "오타 3개 고쳐 줘",
        "3개 항목을 추가해 줘",
        "표 세 개 고쳐 줘",
        # 열린 문서를 지목해 그 안의 항목을 세는 말이다.
        "열린 문서의 오타를 모두 고쳐 줘",
        "이 문서의 '매출'을 모두 '영업이익'으로 바꿔 줘",
    ],
)
def test_open_document_edit_is_not_forced_to_add_attachments(message: str) -> None:
    raw = _raw(action="markdown_edit", persist=False, attached_edit_targets=[], edit_goal="other")

    assert not _names_flag(_failures(raw, _turn(message)))
    assert not asks_open_document_with_attachments(message, 2)


@pytest.mark.parametrize("message", [BUG_MESSAGE, "열린 문서와 첨부 문서 둘 다 고쳐 줘"])
def test_open_document_only_route_retries_when_attachments_are_included_too(message: str) -> None:
    raw = _raw(action="markdown_edit", persist=False, attached_edit_targets=[], edit_goal="other")

    assert any("attached_edit_targets" in failure for failure in _failures(raw, _turn(message)))


def test_summary_of_both_is_not_treated_as_an_edit() -> None:
    raw = _raw(
        action="chat_answer", persist=False, retrieval_source="workspace", document_operation="none",
        required_capabilities=[], edit_goal=None, edit_operation=None, edit_destination=None, attached_edit_targets=[],
    )

    assert _failures(raw, _turn("열린 문서와 첨부 문서 둘 다 요약해 줘")) == []


def test_flag_requires_attached_targets() -> None:
    raw = _raw(attached_edit_targets=[], include_active_markdown=True, edit_goal="other")

    assert _names_flag(_failures(raw, _turn(BUG_MESSAGE)))


@pytest.mark.parametrize("opened", [None, ActiveMarkdownContext(markdown="  \n")], ids=["none", "empty_note"])
def test_turn_without_open_document_is_unchanged(opened: ActiveMarkdownContext | None) -> None:
    request = _turn(BUG_MESSAGE, opened=opened)

    assert _failures(_attachments_only(), request) == []
    # 라우터가 값을 잘못 내도 대상이 없으니 지운다.
    route, _ = _normalize_route(_both())
    assert _repaired_route(route, request).include_active_markdown is False


def test_router_retries_then_includes_open_document() -> None:
    client = SequenceClient(_attachments_only(), _both())

    route = ChatCompletionsTurnRouter(client, "system").route(_turn(BUG_MESSAGE))  # type: ignore[arg-type]

    assert (route.attached_edit_targets, route.include_active_markdown) == ((1, 2), True)
    assert _names_flag(json.loads(client.user_prompts[1])["contract_failures"])


def test_router_fails_loudly_rather_than_dropping_open_document() -> None:
    client = SequenceClient(_attachments_only(), _attachments_only())

    with pytest.raises(AgentTurnRouteContractError):
        ChatCompletionsTurnRouter(client, "system").route(_turn(BUG_MESSAGE))  # type: ignore[arg-type]


def test_payload_gives_open_document_title_only_with_attachments() -> None:
    with_attachments = route_payload(_turn("고쳐 줘", active_document_title="기획서"))
    without_attachments = route_payload(
        AgentTurnRequest(message="고쳐 줘", active_markdown_context=OPEN, active_document_title="기획서")
    )

    assert with_attachments["active_markdown_context"]["title"] == "기획서"
    assert "title" not in without_attachments["active_markdown_context"]


def test_response_carries_flag() -> None:
    route, _ = _normalize_route(_both())

    response = _to_response(AgentTurnResult(action="workspace_workflow", route=route))

    assert response.route.include_active_markdown is True


# 계획 생성


def test_open_document_joins_plan_first_with_its_server_revision() -> None:
    opened = _document("doc-1", "열린 문서", edit_revision=11)
    reader = Reader(opened, _document("doc-2", "둘"), _document("doc-3", "셋"))
    use_case, starter, editor = _use_case(_route(1, 2, include_active_markdown=True), reader)

    result = use_case.execute(_turn(BUG_MESSAGE))

    assert (result.action, result.run_id, result.message) == ("workspace_workflow", "run-1", None)
    contents = starter.requests[0].contents
    assert [content.document_id for content in contents] == ["doc-1", "doc-2", "doc-3"]
    assert all(content.purpose == "apply_document_edit" for content in contents)
    # 편집기 사본이 아니라 서버 본문과 같은 행의 revision을 쓴다. 다른 첨부와 같이 병합·겹침 판정을 받는다.
    assert (contents[0].base_version, contents[0].display_name) == (11, "열린 문서")
    assert contents[0].markdown == opened.markdown + " 수정"
    assert contents[0].preview_json
    assert len(editor.requests) == 3


def test_each_document_is_told_its_share_of_all_targets_including_open_document() -> None:
    reader = Reader(_document("doc-1", "열린 문서"), _document("doc-2", "둘"), _document("doc-3", "셋"))
    use_case, _, editor = _use_case(_route(1, 2, include_active_markdown=True), reader)

    use_case.execute(_turn(BUG_MESSAGE))

    assert "문서 3개" in editor.requests[0].instruction and "3개 중 1번째" in editor.requests[0].instruction
    assert "3개 중 3번째" in editor.requests[2].instruction
    assert "첨부" not in editor.requests[0].instruction.split("[적용 범위]")[1]


def test_open_document_that_is_also_attached_is_edited_once() -> None:
    reader = Reader(_document("doc-1", "열린 문서"), _document("doc-2", "둘"))
    use_case, starter, editor = _use_case(_route(1, 2, include_active_markdown=True), reader)

    use_case.execute(_turn("모두 고쳐 줘", "doc-1", "doc-2"))

    assert [content.document_id for content in starter.requests[0].contents] == ["doc-1", "doc-2"]
    assert len(editor.requests) == 2


def test_attached_open_document_that_was_not_targeted_stops_being_evidence() -> None:
    reader = Reader(_document("doc-1", "열린 문서"), _document("doc-2", "둘"))
    use_case, starter, editor = _use_case(_route(2, include_active_markdown=True), reader)

    use_case.execute(_turn("모두 고쳐 줘", "doc-1", "doc-2"))

    assert [content.document_id for content in starter.requests[0].contents] == ["doc-1", "doc-2"]
    assert editor.requests[0].reference_context == {}


def test_without_flag_open_document_is_left_alone() -> None:
    reader = Reader(_document("doc-1", "열린 문서"), _document("doc-2", "둘"), _document("doc-3", "셋"))
    use_case, starter, _ = _use_case(_route(1, 2), reader)

    use_case.execute(_turn("첨부 문서 둘 다 고쳐 줘"))

    assert [content.document_id for content in starter.requests[0].contents] == ["doc-2", "doc-3"]


@pytest.mark.parametrize(
    "blocked",
    [
        _document("doc-1", "열린 문서", can_edit=False),
        _document("doc-1", "열린 문서", source_blocks=(("b1", "원본"),), edit_revision=None, can_edit=False),
    ],
    ids=["viewer_or_chat_export", "original"],
)
def test_uneditable_open_document_is_named_in_notice(blocked: object) -> None:
    reader = Reader(blocked, _document("doc-2", "둘"), _document("doc-3", "셋"))  # type: ignore[arg-type]
    use_case, starter, _ = _use_case(_route(1, 2, include_active_markdown=True), reader)

    result = use_case.execute(_turn(BUG_MESSAGE))

    assert [content.document_id for content in starter.requests[0].contents] == ["doc-2", "doc-3"]
    assert "‘열린 문서’은(는) 편집 권한이 없어" in (result.message or "")


def test_unsaved_open_document_is_named_in_notice_and_others_proceed() -> None:
    reader = Reader(_document("doc-1", "첫째"), _document("doc-2", "둘째"))
    use_case, starter, _ = _use_case(_route(1, 2, include_active_markdown=True), reader)
    request = replace(_turn(BUG_MESSAGE, "doc-1", "doc-2"), document_id=None, base_version=None)

    result = use_case.execute(request)

    assert [content.document_id for content in starter.requests[0].contents] == ["doc-1", "doc-2"]
    assert "‘열린 문서’은(는) 아직 저장되지 않아" in (result.message or "")


def test_nothing_left_after_unsaved_open_document_explains_why() -> None:
    reader = Reader(_document("doc-1", "첫째", can_edit=False))
    use_case, starter, _ = _use_case(_route(1, include_active_markdown=True), reader)
    request = replace(_turn(BUG_MESSAGE, "doc-1"), document_id=None, base_version=None)

    result = use_case.execute(request)

    assert result.action == "clarify"
    assert starter.requests == []
    assert "아직 저장되지 않아" in (result.message or "")
    assert "편집 권한이 없어" in (result.message or "")


def test_open_document_with_nothing_to_change_is_named_in_notice() -> None:
    opened = _document("doc-1", "열린 문서")
    reader = Reader(opened, _document("doc-2", "둘"))
    use_case, starter, _ = _use_case(
        _route(1, include_active_markdown=True), reader, Editor({opened.markdown: opened.markdown})
    )

    result = use_case.execute(_turn("모두 고쳐 줘", "doc-2"))

    assert [content.document_id for content in starter.requests[0].contents] == ["doc-2"]
    assert "‘열린 문서’은(는) 바꿀 내용이 없어" in (result.message or "")


def test_open_document_title_is_read_for_router_on_attachment_turns() -> None:
    reader = Reader(_document("doc-1", "기획서"), _document("doc-2", "둘"))
    use_case, _, _ = _use_case(_route(1), reader)
    seen: list[AgentTurnRequest] = []
    use_case._router = type("Spy", (), {"route": lambda _, request: seen.append(request) or _route(1)})()

    use_case.execute(_turn("둘째를 고쳐 줘", "doc-2"))

    assert seen[0].active_document_title == "기획서"
    assert seen[0].reference_document_titles == ("둘",)
