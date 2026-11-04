"""첨부가 있는 턴의 의도 판단: 요약·번역이 문서를 덮어쓰지 않고, 여러 첨부 수정과 저장이 끝까지 간다."""

from pathlib import Path

import pytest

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.attached_edit_request import (
    attached_edit_instruction,
    lacks_attachment_change_request,
    lacks_save_or_change_request,
)
from app.modules.agent.domain.entities import ActiveMarkdownContext, AgentTurnRequest, AgentTurnRoute
from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    ChatCompletionsTurnRouter,
    _normalize_route,
    _route_failures,
    repair_create_route_fields,
    route_payload,
)
from app.modules.markdown_edit.application.generate_markdown_document import GenerateMarkdownDocumentUseCase
from app.modules.markdown_edit.application.generate_markdown_edit import GenerateMarkdownEditUseCase
from app.modules.markdown_edit.domain.entities import MarkdownEditTarget
from tests.modules.agent.test_attached_document_edits import Editor, Reader, _document, _route
from tests.modules.agent.test_handle_agent_turn import (
    FakeQueryUseCase,
    FixedRouter,
    RecordingAgentRunStarter,
    RecordingConversationReplier,
)

OPEN_DOCUMENT = ActiveMarkdownContext(markdown="# 열린 문서\n열린 문서 본문")
BLANK_DOCUMENT = ActiveMarkdownContext(
    markdown="", target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1)
)
PROMPTS = Path(__file__).resolve().parents[3] / "prompts"


def _edit_raw(action: str = "markdown_edit", **values: object) -> dict[str, object]:
    return {
        "action": action,
        "confidence": 0.9,
        "reason": "편집",
        "retrieval_source": "none",
        "document_operation": "edit",
        "persist": action == "workspace_workflow",
        "required_capabilities": ["document-edit"],
        "edit_goal": "shorten",
        "edit_operation": "replace",
        "edit_destination": "target",
        "selected_skill_id": None,
        "skill_candidates": [],
        "notice_kind": None,
        "attached_edit_targets": [],
        **values,
    }


def _answer_raw() -> dict[str, object]:
    return _edit_raw(
        action="chat_answer",
        retrieval_source="workspace",
        document_operation="none",
        required_capabilities=[],
        edit_goal=None,
        edit_operation=None,
        edit_destination=None,
    )


def _create_raw(**values: object) -> dict[str, object]:
    return _edit_raw(
        action="workspace_workflow",
        **{
            "document_operation": "create",
            "required_capabilities": ["document-create"],
            "edit_goal": "create_from_chat",
            "edit_operation": None,
            "edit_destination": None,
            **values,
        },
    )


def _request(message: str, *attachments: str, **values: object) -> AgentTurnRequest:
    return AgentTurnRequest(
        message=message,
        workspace_id="workspace-1",
        user_id="user-1",
        reference_document_ids=attachments,
        **{"active_markdown_context": OPEN_DOCUMENT, "document_id": "open-doc", "base_version": 3, **values},
    )


def _failures(raw: dict[str, object], request: AgentTurnRequest) -> list[str]:
    route, failures = _normalize_route(raw)
    return [*failures, *_route_failures(route, request)]


def _mentions_overwrite(failures: list[str]) -> bool:
    return any("explicitly ask to change" in failure for failure in failures)


@pytest.mark.parametrize("action", ["markdown_edit", "workspace_workflow"])
@pytest.mark.parametrize(
    "message",
    [
        "이 문서 세 줄로 요약해 줘",
        "열린 문서를 요약해 줘",
        "요약해서 저장해 줘",
        "영어로 번역해 줘",
        "원문은 고치지 말고 요약해 줘",
    ],
)
def test_summary_on_attachment_turn_cannot_edit_open_document(action: str, message: str) -> None:
    assert _mentions_overwrite(_failures(_edit_raw(action), _request(message, "doc-1")))


@pytest.mark.parametrize(
    ("message", "edit_goal"),
    [
        ("열린 문서를 세 줄로 줄여서 고쳐 줘", "shorten"),
        ("이 문서를 더 짧게 줄여 줘", "shorten"),
        ("첨부한 회의록을 요약해서 이 문서에 넣어 줘", "other"),
        ("첨부 내용을 요약해서 문서 끝에 추가해 줘", "other"),
        ("첨부 내용을 요약해서 이 문서에 적어 줘", "other"),
        ("첨부 내용을 번역해서 문서에 붙여 줘", "other"),
        ("첨부 내용을 반영해서 이 문서를 고쳐 줘", "other"),
        ("이 문서의 요약 섹션을 보강해 줘", "other"),
    ],
)
def test_explicit_change_on_attachment_turn_keeps_open_document_edit(message: str, edit_goal: str) -> None:
    raw = _edit_raw(edit_goal=edit_goal)

    assert not _mentions_overwrite(_failures(raw, _request(message, "doc-1")))


@pytest.mark.parametrize("message", ["열린 문서를 세 줄로 줄여서 고쳐 줘", "세 줄로 줄여서 고쳐 줘", "이 문서를 고쳐 줘"])
def test_request_not_naming_attachment_cannot_target_it_while_document_is_open(message: str) -> None:
    raw = _edit_raw("workspace_workflow", attached_edit_targets=[1])
    request = _request(message, "doc-1", reference_document_titles=("회의록",))

    assert any("must name an attachment" in failure for failure in _failures(raw, request))


@pytest.mark.parametrize(
    "message",
    [
        "첨부한 문서를 현재 문서 형식으로 고쳐 줘",
        "회의록을 현재 문서 형식으로 고쳐 줘",
        "둘 다 오타를 고쳐 줘",
        "3개 모두 오타를 고쳐 줘",
        "첫 번째 문서의 오타를 고쳐 줘",
        "Fix the typos in both documents",
    ],
)
def test_request_naming_attachment_too_keeps_attachment_target(message: str) -> None:
    raw = _edit_raw("workspace_workflow", attached_edit_targets=[1], edit_goal="other")
    request = _request(message, "doc-1", reference_document_titles=("회의록",))

    assert _failures(raw, request) == []


@pytest.mark.parametrize("action", ["markdown_edit", "workspace_workflow"])
@pytest.mark.parametrize(
    "message",
    [
        "문서에 적어 둔 내용을 요약해 줘",
        "문서에 써 있는 메모 요약해 줘",
        "문서 끝에 적어 둔 메모를 번역해 줘",
        "위에 써 있는 내용을 요약해 줘",
        "문서에 붙여 둔 표를 요약해 줘",
        "문서에 적어 놓은 내용을 요약해 줘",
        "문서에 적어 주신 내용을 요약해 줘",
        "문서에 적어 주지 말고 요약해 줘",
        "요약해 줘. 문서에 써 주지 마",
    ],
)
def test_words_describing_existing_text_are_not_a_change_request(action: str, message: str) -> None:
    assert _mentions_overwrite(_failures(_edit_raw(action, edit_goal="other"), _request(message, "doc-1")))


def test_words_describing_existing_text_cannot_start_saving_run_without_attachment() -> None:
    failures = _failures(_edit_raw("workspace_workflow"), _request("문서에 적어 둔 내용을 요약해 줘"))

    assert any("save" in failure for failure in failures)


@pytest.mark.parametrize(
    "message",
    [
        "첨부 내용을 요약해서 문서에 적어 줘",
        "첨부 내용을 요약해서 문서에 적어줘",
        "첨부 내용을 요약해서 문서에 적어",
        "첨부 내용을 요약해서 문서에 적어.",
        "첨부 내용을 요약해서 문서에 적어요",
        "첨부 내용을 요약해서 문서에 써 줘",
        "첨부 내용을 요약해서 문서에 써줘",
        "첨부 내용을 요약해서 문서에 써 주세요",
        "첨부 내용을 요약해서 문서에 써 주시겠어요?",
        "첨부 내용을 요약해서 문서에 써 줄래?",
        "첨부 내용을 요약해서 문서에 적어 둬",
        "첨부 내용을 요약해서 문서에 적어 두세요",
        "첨부 내용을 요약해서 문서에 적어 놔",
        "첨부 내용을 요약해서 문서에 적어 놓아 줘",
        "첨부 내용을 요약해서 문서에 적어 봐",
        "첨부 내용을 요약해서 문서에 적어서 저장해 줘",
        "첨부 내용을 요약해서 문서에 적어서 줘",
        "첨부 내용을 요약해서 문서에 적어 달라고 했잖아",
        "첨부 내용을 요약해서 문서에 적어야 해",
        "첨부 내용을 요약해서 문서 끝에 달아 줘",
        "첨부 내용을 번역해서 문서에 붙여 줘",
        "첨부 내용을 번역해서 문서에 붙여넣기 해 줘",
    ],
)
def test_imperative_write_clause_keeps_open_document_edit(message: str) -> None:
    raw = _edit_raw(edit_goal="other")

    assert not _mentions_overwrite(_failures(raw, _request(message, "doc-1")))


@pytest.mark.parametrize(
    ("message", "edit_goal"),
    [
        ("문서에 이 내용을 적어 줘", "other"),
        ("문서에 추가해 줘", "other"),
        ("요약 섹션을 보강해 줘", "other"),
        ("세 줄로 줄여서 고쳐 줘", "shorten"),
    ],
)
def test_plain_edit_requests_keep_open_document_edit(message: str, edit_goal: str) -> None:
    raw = _edit_raw(edit_goal=edit_goal)

    assert not _mentions_overwrite(_failures(raw, _request(message, "doc-1")))


@pytest.mark.parametrize(
    "message",
    [
        "열린 문서의 오타 3개를 고쳐 줘",
        "열린 문서의 오타를 모두 고쳐 줘",
        "현재 문서의 오타를 전부 고쳐 줘",
        "열린 문서의 두 번째 문단을 고쳐 줘",
        "열린 문서의 단락을 각각 고쳐 줘",
        "열린 문서의 제목과 본문 둘 다 고쳐 줘",
        "Fix all typos in the open document",
        "Fix each heading in the current document",
    ],
)
def test_counting_words_about_open_document_do_not_select_attachment(message: str) -> None:
    raw = _edit_raw("workspace_workflow", attached_edit_targets=[1], edit_goal="other")
    request = _request(message, "doc-1", reference_document_titles=("회의록",))

    assert any("must name an attachment" in failure for failure in _failures(raw, request))


@pytest.mark.parametrize(
    "message",
    [
        "3개 모두 고쳐 줘",
        "각각에서 오타를 고쳐 줘",
        "첨부한 문서 모두 고쳐 줘",
        "둘 다 수정해 줘",
        "두 번째 문서를 고쳐 줘",
        "Fix typos in all attached documents",
        "현재 문서 형식에 맞춰 두 번째 문서를 고쳐 줘",
        "열린 문서 말고 첫 번째 파일을 고쳐 줘",
        "열린 문서를 참고해서 첨부한 문서를 모두 고쳐 줘",
    ],
)
def test_selecting_attachments_keeps_attachment_target(message: str) -> None:
    raw = _edit_raw("workspace_workflow", attached_edit_targets=[1], edit_goal="other")
    request = _request(message, "doc-1", "doc-2", "doc-3", reference_document_titles=("회의록",))

    assert _failures(raw, request) == []


@pytest.mark.parametrize("action", ["markdown_edit", "workspace_workflow"])
@pytest.mark.parametrize(
    "message",
    [
        "수정하는 방법을 요약해 줘",
        "변경하신 내용을 요약해 줘",
        "문서에 넣어 둔 메모를 요약해 줘",
        "고쳐 둔 부분을 요약해 줘",
        "추가해 둔 내용을 번역해 줘",
        "문서에 적어 두고 안 본 메모를 요약해 줘",
        "수정하고 싶은 부분을 요약해 줘",
        "문서에 넣어 둔 걸로 요약해 줘",
        "문서에 넣어 둔 메모를 세 줄로 요약해 줘",
        "고치고 나서 달라진 점을 요약해 줘",
        "수정하면서 바뀐 점을 요약해 줘",
        "수정해 주신 내용을 요약해서 알려 줘",
        "변경해 달라고 한 내용을 요약해 줘",
        "수정해야 할 부분을 번역해 줘",
        "고쳐 주지 말고 요약해 줘",
    ],
)
def test_change_word_modifying_what_to_summarize_is_not_a_change_request(action: str, message: str) -> None:
    assert _mentions_overwrite(_failures(_edit_raw(action, edit_goal="other"), _request(message, "doc-1")))


@pytest.mark.parametrize(
    "message",
    [
        "이 문서에 요약을 적어 줘",
        "문서 끝에 세 줄 요약을 적어 줘",
        "문서 끝에 번역본을 붙여 주세요",
        "요약해서 고쳐 줘",
        "요약해서 문서에 넣어 줘",
        "요약 내용을 문서에 넣어 줘",
        "오타를 고치고 요약해 줘",
        "오타를 고쳐서 요약해 줘",
        "오타를 고쳐 요약해 줘",
        "오타를 고쳐 세 줄로 요약해 줘",
        "오타를 수정해 영어로 번역해 줘",
        "오타를 고쳐 두고 세 줄로 요약해 줘",
        "오타를 고치고 나서 세 줄로 요약해 줘",
        "오타를 수정하면서 세 줄로 요약해 줘",
        "오타를 고쳐 두고 요약해 줘",
        "오타를 고치고 나서 요약해 줘",
        "오타를 수정하면서 요약해 줘",
        "오타를 수정하고 요약해 줘",
        "오타를 수정해 주고 요약해 줘",
        "문서를 고쳐 줘. 그리고 요약해 줘",
        "Fix the typos in it. Then summarize it",
    ],
)
def test_change_clause_counts_around_summary_word(message: str) -> None:
    raw = _edit_raw(edit_goal="other")

    assert not _mentions_overwrite(_failures(raw, _request(message, "doc-1")))


@pytest.mark.parametrize(
    "message",
    [
        "문서에 있는 요약을 적어 줘",
        "문서에 적어 둔 요약을 적어 줘",
        "요약을 적어 줘",
        "문서에 있는 내용을 요약해서 적어 줘",
    ],
)
def test_write_clause_without_document_as_destination_stays_separate_result(message: str) -> None:
    assert _mentions_overwrite(_failures(_edit_raw(edit_goal="other"), _request(message, "doc-1")))


@pytest.mark.parametrize(
    ("message", "lacks_change", "lacks_save_or_change"),
    [
        ("요약해서 저장해 줘", True, False),
        ("번역해서 저장해 줘", True, False),
    ],
)
def test_summary_with_save_clause_keeps_new_document_path(message: str, lacks_change: bool, lacks_save_or_change: bool) -> None:
    assert lacks_attachment_change_request(message, "other") is lacks_change
    assert lacks_save_or_change_request(message, "other") is lacks_save_or_change


def test_write_then_move_keeps_current_verdict() -> None:
    assert not lacks_attachment_change_request("적어 두고 폴더로 옮겨 줘", "other")
    assert lacks_attachment_change_request("적어 두고 폴더로 옮겨 줘", "shorten")
    assert not lacks_attachment_change_request("문서에 적어 두고 폴더로 옮겨 줘", "shorten")


@pytest.mark.parametrize(
    "message",
    [
        "첨부 내용대로 열린 문서의 오타를 모두 고쳐 줘",
        "열린 문서를 첨부 내용대로 고쳐 줘",
        "첨부한 문서를 참고해서 열린 문서의 오타를 고쳐 줘",
        "첨부 내용을 반영해서 현재 문서를 고쳐 줘",
        "첨부 문서 말고 열린 문서를 고쳐 줘",
    ],
)
def test_attachment_only_as_source_does_not_select_it_when_open_document_named(message: str) -> None:
    raw = _edit_raw("workspace_workflow", attached_edit_targets=[1], edit_goal="other")
    request = _request(message, "doc-1", reference_document_titles=("회의록",))

    assert any("must name an attachment" in failure for failure in _failures(raw, request))


@pytest.mark.parametrize(
    "message",
    [
        "첨부 문서를 고쳐 줘",
        "첨부 내용대로 고쳐 줘",
        "첨부 내용대로 열린 문서와 첨부 문서를 모두 고쳐 줘",
        "첨부한 문서를 열린 문서 기준으로 고쳐 줘",
        "열린 문서와 첨부한 문서를 고쳐 줘",
        "회의록 내용대로 열린 문서를 고쳐 줘",
    ],
)
def test_changing_attachment_itself_keeps_attachment_target(message: str) -> None:
    raw = _edit_raw("workspace_workflow", attached_edit_targets=[1], edit_goal="other")
    request = _request(message, "doc-1", reference_document_titles=("회의록",))

    assert _failures(raw, request) == []


def test_request_naming_blank_open_document_keeps_attachment_target() -> None:
    raw = _edit_raw("workspace_workflow", attached_edit_targets=[1])
    request = _request("현재 문서를 세 줄로 줄여서 고쳐 줘", "doc-1", active_markdown_context=BLANK_DOCUMENT)

    assert _failures(raw, request) == []


def test_guard_makes_router_retry_and_land_on_answer() -> None:
    client = _Sequence(_edit_raw(), _answer_raw())

    route = ChatCompletionsTurnRouter(client, system_prompt="라우터").route(_request("이 문서 세 줄로 요약해 줘", "doc-1"))

    assert (route.action, route.document_operation) == ("chat_answer", "none")
    assert len(client.calls) == 2


OPEN_ONLY_ANSWERS = ["이 문서 세 줄로 요약해 줘", "이 문서 요약해 줘", "이 문서를 영어로 번역해 줘"]
OPEN_ONLY_EDITS = [
    ("오타 고쳐 줘", "cleanup"),
    ("세 줄로 줄여서 고쳐 줘", "shorten"),
    ("요약 섹션을 보강해 줘", "other"),
    ("요약해서 고쳐 줘", "shorten"),
    ("이 문서를 영어로 번역해서 바꿔 줘", "translate"),
]
SAVE_SUMMARY = "현재 문서를 요약해서 저장해 줘"


def _reply_raw() -> dict[str, object]:
    return {**_answer_raw(), "action": "conversation_reply", "retrieval_source": "none"}


@pytest.mark.parametrize("mode", [None, "task"])
@pytest.mark.parametrize("message", OPEN_ONLY_ANSWERS)
def test_open_document_only_summary_cannot_edit_it(message: str, mode: str | None) -> None:
    """프론트가 markdown_edit을 자동 승인으로 바로 적용하므로 미리 보기라도 수정으로 받지 않는다."""
    assert _mentions_overwrite(_failures(_edit_raw(), _request(message, mode=mode)))


@pytest.mark.parametrize("mode", [None, "task"])
@pytest.mark.parametrize(("message", "edit_goal"), OPEN_ONLY_EDITS)
def test_open_document_only_change_request_keeps_edit(message: str, edit_goal: str, mode: str | None) -> None:
    assert _failures(_edit_raw(edit_goal=edit_goal), _request(message, mode=mode)) == []


@pytest.mark.parametrize("message", ["한 문단으로 압축해 줘", "세 줄로 정리해 줘", "수정 사항을 표로 정리해 줘"])
def test_open_document_only_edit_without_summary_word_stays_with_the_router(message: str) -> None:
    """현재 경계를 기록한다. 요약·번역이라는 말이 없으면 라우터가 고른 edit_goal이 shorten이어도 막지 않는다."""
    assert _failures(_edit_raw(), _request(message, mode="task")) == []


@pytest.mark.parametrize("mode", [None, "task"])
@pytest.mark.parametrize("message", OPEN_ONLY_ANSWERS)
def test_open_document_only_summary_answer_passes_contract(message: str, mode: str | None) -> None:
    assert _failures(_answer_raw(), _request(message, mode=mode)) == []
    assert _failures(_reply_raw(), _request(message, mode=mode)) == []


@pytest.mark.parametrize("message", OPEN_ONLY_ANSWERS)
def test_open_document_only_summary_retries_and_lands_on_answer_in_task_mode(message: str) -> None:
    client = _Sequence(_edit_raw(), _answer_raw())

    route = ChatCompletionsTurnRouter(client, system_prompt="라우터").route(_request(message, mode="task"))

    assert (route.action, route.document_operation, route.notice_kind) == ("chat_answer", "none", None)
    assert len(client.calls) == 2


def test_open_document_only_summary_never_returns_edit_when_model_insists() -> None:
    client = _Sequence(_edit_raw(), _edit_raw())

    with pytest.raises(AgentTurnRouteContractError):
        ChatCompletionsTurnRouter(client, system_prompt="라우터").route(_request("이 문서 요약해 줘", mode="task"))

    assert len(client.calls) == 2


def test_open_document_only_summary_chat_tab_clarify_becomes_answer() -> None:
    clarify = {**_answer_raw(), "action": "clarify", "retrieval_source": "none", "notice_kind": "chat_tab"}

    route = ChatCompletionsTurnRouter(_Sequence(clarify), system_prompt="라우터").route(
        _request("이 문서 요약해 줘", mode="task")
    )

    assert (route.action, route.retrieval_source, route.notice_kind) == ("chat_answer", "workspace", None)


def test_summary_without_open_document_keeps_document_route() -> None:
    """고칠 문서가 없다는 안내는 실행 단계가 한다. 답변으로 바꾸면 근거 없는 요약이 나온다."""
    without_document = {"active_markdown_context": None, "document_id": None, "base_version": None}

    assert _failures(_edit_raw(), _request("이 문서 요약해 줘", mode="task", **without_document)) == []
    assert _failures(_edit_raw(), _request("이 문서 요약해 줘", mode="task", active_markdown_context=BLANK_DOCUMENT)) == []
    assert any("task mode" in failure for failure in _failures(_answer_raw(), _request("이 문서 요약해 줘", mode="task", **without_document)))


def test_open_document_only_question_still_goes_to_chat_tab() -> None:
    route = ChatCompletionsTurnRouter(_Sequence(_answer_raw()), system_prompt="라우터").route(
        _request("이 문서에서 다음 회의가 언제야?", mode="task")
    )

    assert (route.action, route.notice_kind) == ("clarify", "chat_tab")


@pytest.mark.parametrize("action", ["workspace_workflow", "markdown_edit"])
def test_open_document_only_summary_save_clause_stays_an_edit_of_the_open_document(action: str) -> None:
    """현재 동작을 기록한다. 원문을 덮어쓰는 이 동작을 유지할지는 사용자 결정을 기다린다."""
    request = _request(SAVE_SUMMARY, mode="task")

    route = ChatCompletionsTurnRouter(_Sequence(_edit_raw(action)), system_prompt="라우터").route(request)

    assert (route.action, route.document_operation, route.edit_goal) == (action, "edit", "shorten")
    assert (route.edit_operation, route.edit_destination) == ("replace", "target")


def test_open_document_only_answer_pins_the_open_document_without_editing() -> None:
    reader = Reader()
    use_case, query = _answer_use_case(ANSWER_ROUTE, reader)

    result = use_case.execute(_request("이 문서 세 줄로 요약해 줘", mode="task"))

    assert (result.action, result.edit, result.run_id) == ("chat_answer", None, None)
    pinned = query.kwargs[0]["pinned_evidence"]
    assert {snippet.source_document_id for snippet in pinned} == {"open-doc"}
    assert "열린 문서 본문" in pinned[0].text
    assert reader.body_calls == []


def test_open_document_only_answer_covers_only_the_selection() -> None:
    """편집이 선택 범위만 고치던 것처럼 답변도 선택한 범위로 답한다."""
    context = ActiveMarkdownContext(
        markdown="# 제목\n첫 줄\n둘째 줄",
        target=MarkdownEditTarget(type="selection", start_line=2, end_line=2),
    )
    use_case, query = _answer_use_case(ANSWER_ROUTE, Reader())

    use_case.execute(_request("선택한 부분 요약해 줘", mode="task", active_markdown_context=context))

    pinned_text = " ".join(snippet.text for snippet in query.kwargs[0]["pinned_evidence"])
    assert "첫 줄" in pinned_text
    assert "둘째 줄" not in pinned_text


def test_open_document_only_reply_gets_the_open_document_as_reference() -> None:
    replier = RecordingConversationReplier("번역입니다")
    use_case, _ = _answer_use_case(
        AgentTurnRoute(action="conversation_reply", confidence=1.0, reason="번역"),
        Reader(),
        conversation_replier=replier,
    )

    result = use_case.execute(_request("이 문서를 영어로 번역해 줘", mode="task"))

    assert (result.action, result.edit, result.run_id) == ("conversation_reply", None, None)
    context = replier.requests[0].conversation_context.reference_context
    assert [document["markdown"] for document in context["reference_documents"]] == [OPEN_DOCUMENT.markdown]


def test_open_document_only_other_question_pins_nothing() -> None:
    use_case, query = _answer_use_case(ANSWER_ROUTE, Reader())

    use_case.execute(_request("이 문서가 왜 틀렸어?"))

    assert "pinned_evidence" not in query.kwargs[0]


def test_prompt_routes_open_document_summary_to_an_answer() -> None:
    prompt = (PROMPTS / "agent_turn_router.system.md").read_text(encoding="utf-8")

    assert "is `markdown_edit` with edit_goal `shorten`; a question" not in prompt
    assert "Open document without attachments:" in prompt


def test_open_document_only_summary_cannot_start_saving_run_without_save_clause() -> None:
    """workspace_workflow 편집은 자동 승인이면 바로 덮어쓰므로, 저장하라는 말이 있어야 한다."""
    failures = _failures(_edit_raw("workspace_workflow"), _request("이 문서 요약해 줘"))

    assert any("save" in failure for failure in failures)


@pytest.mark.parametrize("message", ["현재 문서를 요약해서 저장해줘", "요약하고 적용해 줘", "이 문서를 세 줄로 줄여서 고쳐 줘"])
def test_open_document_only_explicit_save_or_change_starts_run(message: str) -> None:
    assert _failures(_edit_raw("workspace_workflow"), _request(message)) == []


def test_open_document_only_summary_with_folder_move_keeps_compound_workflow() -> None:
    raw = _edit_raw("workspace_workflow", required_capabilities=["document-edit", "folder-organize"])

    assert _failures(raw, _request("현재 문서를 요약한 뒤 보관 폴더로 옮겨 줘")) == []


def test_blank_open_document_looks_like_no_open_document_to_router() -> None:
    with_chip = route_payload(_request("세 줄로 줄여서 고쳐 줘", "doc-1", active_markdown_context=BLANK_DOCUMENT))
    without_chip = route_payload(
        _request("세 줄로 줄여서 고쳐 줘", "doc-1", active_markdown_context=None, document_id=None, base_version=None)
    )

    assert with_chip == without_chip


def test_blank_open_document_without_attachment_keeps_its_target() -> None:
    payload = route_payload(_request("문서를 고쳐 줘", active_markdown_context=BLANK_DOCUMENT))

    assert payload["active_markdown_context"]["target"] is not None


def test_attachment_create_route_with_edit_goal_is_repaired() -> None:
    route, _ = _normalize_route(_create_raw(edit_goal="shorten", edit_operation="replace", edit_destination="target"))

    repaired = repair_create_route_fields(route, _request("요약해서 저장해 줘", "doc-1"))

    assert (repaired.edit_goal, repaired.edit_operation, repaired.edit_destination) == ("create_from_chat", None, None)
    assert _route_failures(repaired, _request("요약해서 저장해 줘", "doc-1")) == []


def test_create_route_without_attachment_is_not_repaired() -> None:
    route, _ = _normalize_route(_create_raw(edit_goal="shorten"))

    assert repair_create_route_fields(route, _request("문서를 만들어 저장해 줘")) is route


class _Sequence:
    def __init__(self, *responses: dict[str, object]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
        self.calls.append(user_prompt)
        return self.responses.pop(0)


def _answer_use_case(route: AgentTurnRoute, reader: Reader, **values: object):
    editor = Editor()
    query = FakeQueryUseCase()
    use_case = HandleAgentTurnUseCase(
        router=FixedRouter(route),
        query_use_case=query,  # type: ignore[arg-type]
        markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
        markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
        reference_reader=reader,  # type: ignore[arg-type]
        **values,
    )
    return use_case, query


ANSWER_ROUTE = AgentTurnRoute(action="chat_answer", confidence=1.0, reason="답변", retrieval_source="workspace")


def test_answer_naming_open_document_pins_it_instead_of_attachment() -> None:
    reader = Reader(_document("doc-1", "첨부"))
    use_case, query = _answer_use_case(ANSWER_ROUTE, reader)

    use_case.execute(_request("열린 문서를 요약해 줘", "doc-1"))

    pinned = query.kwargs[0]["pinned_evidence"]
    assert {snippet.source_document_id for snippet in pinned} == {"open-doc"}
    assert "열린 문서 본문" in pinned[0].text
    assert reader.body_calls == []


def test_answer_naming_both_pins_open_document_and_attachment() -> None:
    reader = Reader(_document("doc-1", "첨부"))
    use_case, query = _answer_use_case(ANSWER_ROUTE, reader)

    use_case.execute(_request("열린 문서와 첨부한 문서를 비교해 줘", "doc-1"))

    assert {snippet.source_document_id for snippet in query.kwargs[0]["pinned_evidence"]} == {"open-doc", "doc-1"}


def test_answer_not_naming_open_document_pins_only_attachment() -> None:
    reader = Reader(_document("doc-1", "첨부"))
    use_case, query = _answer_use_case(ANSWER_ROUTE, reader)

    use_case.execute(_request("이 문서 세 줄로 요약해 줘", "doc-1"))

    assert {snippet.source_document_id for snippet in query.kwargs[0]["pinned_evidence"]} == {"doc-1"}


def test_answer_naming_blank_open_document_falls_back_to_attachment() -> None:
    reader = Reader(_document("doc-1", "첨부"))
    use_case, query = _answer_use_case(ANSWER_ROUTE, reader)

    use_case.execute(_request("현재 문서를 요약해 줘", "doc-1", active_markdown_context=BLANK_DOCUMENT))

    assert {snippet.source_document_id for snippet in query.kwargs[0]["pinned_evidence"]} == {"doc-1"}


def test_reply_naming_open_document_gets_it_as_reference() -> None:
    replier = RecordingConversationReplier("번역입니다")
    use_case, _ = _answer_use_case(
        AgentTurnRoute(action="conversation_reply", confidence=1.0, reason="번역"),
        Reader(_document("doc-1", "첨부")),
        conversation_replier=replier,
    )

    use_case.execute(_request("현재 문서를 영어로 번역해 줘", "doc-1"))

    context = replier.requests[0].conversation_context.reference_context
    assert [document["markdown"] for document in context["reference_documents"]] == [OPEN_DOCUMENT.markdown]


def test_several_attachments_get_instruction_scoped_to_one_document() -> None:
    reader = Reader(_document("doc-1", "하나"), _document("doc-2", "둘"), _document("doc-3", "셋"))
    editor = Editor()
    starter = RecordingAgentRunStarter()
    use_case = HandleAgentTurnUseCase(
        router=FixedRouter(_route(1, 2, 3)),
        query_use_case=FakeQueryUseCase(),  # type: ignore[arg-type]
        markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
        markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
        agent_run_starter=starter,  # type: ignore[arg-type]
        reference_reader=reader,  # type: ignore[arg-type]
    )
    message = "첨부한 문서 3개 모두에서 '매출'을 '영업이익'으로 바꿔 줘"

    use_case.execute(_request(message, "doc-1", "doc-2", "doc-3", active_markdown_context=None))

    instructions = [request.instruction for request in editor.requests]
    assert all(instruction.startswith(message) for instruction in instructions)
    assert ["3개 중 1번째" in instructions[0], "3개 중 2번째" in instructions[1], "3개 중 3번째" in instructions[2]] == [True] * 3
    # 사용자에게 보이는 요청 문장과 계획 입력은 사용자의 말 그대로다.
    assert starter.requests[0].instruction == message


def test_single_attachment_instruction_stays_as_written() -> None:
    assert attached_edit_instruction("문서를 고쳐 줘", position=1, total=1) == "문서를 고쳐 줘"


def test_prompts_state_resolved_rules() -> None:
    router = (PROMPTS / "agent_turn_router.system.md").read_text(encoding="utf-8")
    planner = (PROMPTS / "agent_folder_plan.system.md").read_text(encoding="utf-8")

    # 작업 탭 규칙이 첨부 턴에 열린 문서 요약을 편집으로 보내던 문장은 없어야 한다.
    assert "With attached documents, this applies only when the message names the open document" not in router
    assert "요약해서 저장해 줘" in router
    assert "열린 문서를 세 줄로 줄여서 고쳐 줘" in router
    assert "already generated" in planner
    assert "settle the target, not consent" in planner
