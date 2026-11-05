"""보기 권한으로 연 문서가 첨부 맨 앞에 실려도, 편집 대상 판정에서는 후보가 아니다."""

import pytest

from app.modules.agent.domain.attached_document_edits import ALL_FORBIDDEN_MESSAGE
from app.modules.agent.domain.entities import ActiveMarkdownContext, AgentTurnRoute
from app.modules.agent.infrastructure.chat_completions_turn_router import route_payload
from app.modules.skill.domain.exceptions import SkillRequestRejectedError
from tests.modules.agent.test_attached_document_edits import Editor, Reader, _document, _request, _route, _use_case

READ_ONLY = _document("doc-1", "보기전용", can_edit=False)
EDITABLE = _document("doc-2", "편집문서")
OTHER_EDITABLE = _document("doc-3", "다른문서")
CLARIFY_TARGET = AgentTurnRoute(action="clarify", confidence=0.5, reason="어느 문서", notice_kind="attachment_target")
EDIT_WITHOUT_TARGETS = _route(action="markdown_edit", persist=False, edit_goal="cleanup")


def _edited(starter) -> list[str]:
    return [content.document_id for content in starter.requests[0].contents] if starter.requests else []


@pytest.mark.parametrize(
    "route",
    [_route(2), _route(1), CLARIFY_TARGET, EDIT_WITHOUT_TARGETS],
    ids=["model-picks-editable", "model-picks-read-only", "model-asks-which", "model-forgets-targets"],
)
def test_unnamed_change_with_one_editable_attachment_edits_it(route: AgentTurnRoute) -> None:
    use_case, starter, editor = _use_case(route, Reader(READ_ONLY, EDITABLE))

    result = use_case.execute(_request("doc-1", "doc-2", message="오타 고쳐 줘"))

    assert result.action == "workspace_workflow"
    assert _edited(starter) == ["doc-2"]
    assert [item["document_id"] for item in editor.requests[0].reference_context["reference_documents"]] == ["doc-1"]


def test_model_picking_both_edits_the_editable_one_and_says_which_was_left_out() -> None:
    use_case, starter, _ = _use_case(_route(1, 2), Reader(READ_ONLY, EDITABLE))

    result = use_case.execute(_request("doc-1", "doc-2", message="오타 고쳐 줘"))

    assert _edited(starter) == ["doc-2"]
    assert "‘보기전용’은(는) 편집 권한이 없어 제외했어요." in result.message


@pytest.mark.parametrize(
    "route", [_route(1), CLARIFY_TARGET, EDIT_WITHOUT_TARGETS], ids=["targets", "asks-which", "forgets-targets"]
)
def test_change_with_only_read_only_attachment_says_no_permission(route: AgentTurnRoute) -> None:
    use_case, starter, _ = _use_case(route, Reader(READ_ONLY))

    result = use_case.execute(_request("doc-1", message="오타 고쳐 줘"))

    assert (result.action, result.message) == ("clarify", ALL_FORBIDDEN_MESSAGE)
    assert starter.requests == []


@pytest.mark.parametrize(
    "message",
    ["보기전용 오타 고쳐 줘", "첫 번째 문서 오타 고쳐 줘", "1번 오타 고쳐 줘", "이 문서 오타 고쳐 줘", "현재 문서 오타 고쳐 줘"],
)
def test_message_pointing_at_read_only_document_does_not_move_to_another_document(message: str) -> None:
    use_case, starter, _ = _use_case(_route(1), Reader(READ_ONLY, EDITABLE))

    result = use_case.execute(_request("doc-1", "doc-2", message=message))

    assert (result.action, result.message) == ("clarify", ALL_FORBIDDEN_MESSAGE)
    assert starter.requests == []


def test_open_document_cannot_be_the_editable_attachment_when_no_markdown_is_open() -> None:
    """열린 문서가 편집 문맥에 없으면 그 문서는 보기 전용이다. 모델이 첨부를 골라도 다른 문서를 고치지 않는다."""
    use_case, starter, _ = _use_case(_route(2), Reader(READ_ONLY, EDITABLE))

    result = use_case.execute(_request("doc-1", "doc-2", message="열린 문서 오타 고쳐 줘"))

    assert (result.action, result.message) == ("clarify", ALL_FORBIDDEN_MESSAGE)
    assert starter.requests == []


@pytest.mark.parametrize(
    "message", ["열린 문서 형식에 맞춰 첨부 문서 고쳐 줘", "현재 문서 참고해서 편집문서 고쳐 줘"]
)
def test_open_document_as_a_source_keeps_the_chosen_attachment(message: str) -> None:
    use_case, starter, _ = _use_case(_route(2), Reader(READ_ONLY, EDITABLE))

    use_case.execute(_request("doc-1", "doc-2", message=message))

    assert _edited(starter) == ["doc-2"]


def test_selecting_all_edits_every_editable_attachment_and_says_which_was_left_out() -> None:
    use_case, starter, _ = _use_case(_route(1, 2, 3), Reader(READ_ONLY, EDITABLE, OTHER_EDITABLE))

    result = use_case.execute(_request("doc-1", "doc-2", "doc-3", message="3개 모두 고쳐 줘"))

    assert _edited(starter) == ["doc-2", "doc-3"]
    assert "‘보기전용’은(는) 편집 권한이 없어 제외했어요." in result.message


@pytest.mark.parametrize("route", [_route(1), CLARIFY_TARGET, EDIT_WITHOUT_TARGETS])
def test_several_editable_attachments_are_asked_about_without_the_read_only_one(route: AgentTurnRoute) -> None:
    use_case, starter, _ = _use_case(route, Reader(READ_ONLY, EDITABLE, OTHER_EDITABLE))

    result = use_case.execute(_request("doc-1", "doc-2", "doc-3", message="오타 고쳐 줘"))

    assert (result.action, result.clarify_reason) == ("clarify", "attachment_target_required")
    assert result.message == "첨부한 문서 중 어느 문서를 고칠까요? 번호나 제목으로 알려 주세요.\n2. 편집문서\n3. 다른문서"
    assert starter.requests == []


def test_editable_attachments_keep_the_existing_question_and_choice() -> None:
    reader = Reader(EDITABLE, OTHER_EDITABLE)
    use_case, _, _ = _use_case(CLARIFY_TARGET, reader)
    asked = use_case.execute(_request("doc-1", "doc-2", message="오타 고쳐 줘"))
    assert asked.message.endswith("1. 편집문서\n2. 다른문서")

    use_case, starter, _ = _use_case(_route(1), Reader(_document("doc-1", "하나"), _document("doc-2", "둘")))
    use_case.execute(_request("doc-1", "doc-2", message="오타 고쳐 줘"))
    assert _edited(starter) == ["doc-1"]


def test_edit_of_open_document_stays_when_read_only_document_is_attached() -> None:
    route = _route(action="markdown_edit", persist=False, edit_goal="cleanup", attached_edit_targets=())
    use_case, starter, editor = _use_case(route, Reader(READ_ONLY, EDITABLE))

    result = use_case.execute(
        _request(
            "doc-1",
            "doc-2",
            message="오타 고쳐 줘",
            active_markdown_context=ActiveMarkdownContext(markdown="# 열린 문서\n본문"),
            document_id="open-doc",
            base_version=1,
        )
    )

    assert result.action == "markdown_edit"
    assert starter.requests == []
    assert editor.requests[0].markdown == "# 열린 문서\n본문"


def test_summary_routes_keep_read_only_document_as_evidence() -> None:
    route = AgentTurnRoute(action="markdown_create", confidence=1.0, reason="새 문서", document_operation="create", edit_goal="create_from_chat", required_capabilities=("document-create",))

    class CreatingEditor(Editor):
        def __init__(self) -> None:
            super().__init__()
            self.create_requests: list[object] = []

        def generate_markdown(self, request: object) -> object:
            self.create_requests.append(request)
            raise ValueError("요청만 확인한다")

    editor = CreatingEditor()
    use_case, _, _ = _use_case(route, Reader(READ_ONLY, EDITABLE), editor)

    with pytest.raises(ValueError, match="요청만 확인한다"):
        use_case.execute(_request("doc-1", "doc-2", message="요약해서 저장해 줘"))

    documents = editor.create_requests[0].reference_context["reference_documents"]
    assert [item["document_id"] for item in documents] == ["doc-1", "doc-2"]


def test_answer_route_with_read_only_document_is_left_as_is() -> None:
    route = AgentTurnRoute(action="chat_answer", confidence=1.0, reason="질문", retrieval_source="workspace")
    use_case, starter, editor = _use_case(route, Reader(READ_ONLY, EDITABLE))

    result = use_case.execute(_request("doc-1", "doc-2", message="이 문서 요약해 줘"))

    assert result.action == "chat_answer"
    assert starter.requests == []
    assert editor.requests == []


def test_router_input_carries_can_edit_only_when_an_attachment_is_read_only() -> None:
    use_case, _, _ = _use_case(_route(2), Reader(READ_ONLY, EDITABLE))
    use_case.execute(_request("doc-1", "doc-2", message="오타 고쳐 줘"))
    assert route_payload(use_case._router.requests[0])["reference_documents"] == {
        "count": 2,
        "titles": ["보기전용", "편집문서"],
        "can_edit": [False, True],
    }

    use_case, _, _ = _use_case(_route(2), Reader(EDITABLE, OTHER_EDITABLE))
    use_case.execute(_request("doc-1", "doc-2", message="오타 고쳐 줘"))
    assert "can_edit" not in route_payload(use_case._router.requests[0])["reference_documents"]


def test_unknown_edit_state_is_not_treated_as_read_only() -> None:
    """제목을 못 읽었거나 구 document-svc면 모른다. 판정은 본문을 읽는 단계의 기존 안내에 맡긴다."""
    old_service = _document("doc-1", "구버전", edit_revision=None, can_edit=False)
    unreadable = SkillRequestRejectedError("invalid_reference", "없는 문서")
    use_case, starter, _ = _use_case(_route(2), Reader(unreadable, old_service))

    use_case.execute(_request("doc-1", "doc-2", message="오타 고쳐 줘"))

    assert "can_edit" not in route_payload(use_case._router.requests[0])["reference_documents"]
    assert starter.requests == []


def test_original_document_counts_as_read_only() -> None:
    original = _document("doc-1", "원본", edit_revision=None, can_edit=False, source_blocks=(("b1", "본문"),))
    use_case, starter, _ = _use_case(_route(1), Reader(original, EDITABLE))

    use_case.execute(_request("doc-1", "doc-2", message="오타 고쳐 줘"))

    assert _edited(starter) == ["doc-2"]


def test_converted_route_passes_the_router_contract() -> None:
    """라우터 계약과 같은 값을 만들어야 실행 갈래가 그대로 받는다."""
    use_case, starter, editor = _use_case(CLARIFY_TARGET, Reader(READ_ONLY, EDITABLE))

    result = use_case.execute(_request("doc-1", "doc-2", message="오타 고쳐 줘"))

    route = result.route
    assert (route.action, route.document_operation, route.persist) == ("workspace_workflow", "edit", True)
    assert (route.edit_operation, route.edit_destination) == ("replace", "target")
    assert route.required_capabilities == ("document-edit",)
    assert route.notice_kind is None
    assert editor.requests[0].edit_goal == "other"
