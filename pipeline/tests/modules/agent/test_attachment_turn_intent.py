"""첨부가 있는 턴의 의도 판단: 요약·번역이 문서를 덮어쓰지 않고, 여러 첨부 수정과 저장이 끝까지 간다."""

from pathlib import Path

import pytest

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.attached_edit_request import attached_edit_instruction
from app.modules.agent.domain.entities import ActiveMarkdownContext, AgentTurnRequest, AgentTurnRoute
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


def test_request_naming_blank_open_document_keeps_attachment_target() -> None:
    raw = _edit_raw("workspace_workflow", attached_edit_targets=[1])
    request = _request("현재 문서를 세 줄로 줄여서 고쳐 줘", "doc-1", active_markdown_context=BLANK_DOCUMENT)

    assert _failures(raw, request) == []


def test_guard_makes_router_retry_and_land_on_answer() -> None:
    client = _Sequence(_edit_raw(), _answer_raw())

    route = ChatCompletionsTurnRouter(client, system_prompt="라우터").route(_request("이 문서 세 줄로 요약해 줘", "doc-1"))

    assert (route.action, route.document_operation) == ("chat_answer", "none")
    assert len(client.calls) == 2


def test_open_document_only_summary_keeps_preview_edit() -> None:
    """첨부 없는 markdown_edit은 미리 보기뿐이다. 작업 탭에는 답변 갈래가 없어 막으면 요약을 받을 길이 없다."""
    request = _request("이 문서 요약해 줘", mode="task")

    assert _failures(_edit_raw(), request) == []


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
