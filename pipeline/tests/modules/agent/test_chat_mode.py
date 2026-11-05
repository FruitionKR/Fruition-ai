"""채팅 탭 문서 질문(mode=chat) 턴과, 작업 탭(mode=task)에서 질문을 채팅 탭으로 돌려보내는 규칙."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.modules.agent.application.handle_agent_turn import (
    CLARIFY_TASK_MODE_QUESTION_MESSAGE,
    CLARIFY_TASK_TAB_MESSAGE,
    HandleAgentTurnUseCase,
)
from app.modules.agent.domain.entities import ActiveMarkdownContext, AgentTurnRequest, AgentTurnRoute
from app.modules.agent.domain.task_mode import task_mode_route
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    DEFAULT_AGENT_TURN_ROUTER_PROMPT,
    ChatCompletionsTurnRouter,
    _route_failures,
    route_payload,
)
from app.modules.agent.interfaces.http.responses import _to_response
from app.modules.agent.interfaces.http.schemas import AgentTurnRequestBody
from app.modules.markdown_edit.application.generate_markdown_document import GenerateMarkdownDocumentUseCase
from app.modules.markdown_edit.application.generate_markdown_edit import GenerateMarkdownEditUseCase
from app.modules.markdown_edit.domain.entities import MarkdownEditOperation, MarkdownEditResult, MarkdownEditTarget
from app.modules.skill.domain.entities import SkillAuthoringReference
from tests.modules.agent.test_chat_completions_turn_router import SequenceJsonClient, route_response
from tests.modules.agent.test_handle_agent_turn import (
    FakeQueryUseCase,
    FixedRouter,
    RecordingAgentRunStarter,
    RecordingConversationReplier,
    RecordingMarkdownEditor,
)

OPEN_DOCUMENT = {
    "active_markdown_context": ActiveMarkdownContext(markdown="# 열린 문서\n베타 공개일은 11월 20일이다."),
    "document_id": "open-doc",
    "base_version": 3,
}
ATTACHED = {"reference_document_ids": ("doc-1",)}
EDIT_FIELDS = {"edit_operation": "replace", "edit_destination": "target"}

ANSWER = AgentTurnRoute(action="chat_answer", confidence=0.9, reason="question", retrieval_source="workspace")
REPLY = AgentTurnRoute(action="conversation_reply", confidence=0.9, reason="reply")
EDIT = AgentTurnRoute(
    action="markdown_edit", confidence=0.9, reason="edit", document_operation="edit", edit_goal="cleanup",
    required_capabilities=("document-edit",), **EDIT_FIELDS,  # type: ignore[arg-type]
)
ATTACHED_EDIT = replace(
    EDIT, action="workspace_workflow", persist=True, attached_edit_targets=(1,), include_active_markdown=True
)
SAVE_EDIT = replace(EDIT, action="workspace_workflow", persist=True, edit_goal="shorten")
CREATE = AgentTurnRoute(
    action="markdown_create", confidence=0.9, reason="create", document_operation="create",
    edit_goal="create_from_chat", required_capabilities=("document-create",),
)
SAVE_CREATE = replace(CREATE, action="workspace_workflow", persist=True)
FOLDER_ORGANIZE = AgentTurnRoute(
    action="folder_organize", confidence=0.9, reason="folder", persist=True, required_capabilities=("folder-organize",)
)
FOLDER_LOOKUP = AgentTurnRoute(action="folder_lookup", confidence=0.9, reason="lookup")
SKILL = AgentTurnRoute(action="skill_authoring", confidence=0.9, reason="skill")
SKILL_DRAFT = AgentTurnRoute(action="skill_draft_proposal", confidence=0.9, reason="skill")
ATTACHMENT_TARGET = AgentTurnRoute(action="clarify", confidence=0.9, reason="which", notice_kind="attachment_target")
EDIT_CLARIFY = replace(EDIT, action="clarify")

CHANGING_ROUTES = (
    EDIT, ATTACHED_EDIT, SAVE_EDIT, CREATE, SAVE_CREATE, FOLDER_ORGANIZE, FOLDER_LOOKUP, SKILL, SKILL_DRAFT,
    ATTACHMENT_TARGET, EDIT_CLARIFY,
)


class FakeReader:
    def __init__(self) -> None:
        self.reads: list[str] = []

    def read(self, **kwargs: object) -> SkillAuthoringReference:
        self.reads.append(str(kwargs["document_id"]))
        return SkillAuthoringReference(id=str(kwargs["document_id"]), name="회의록", markdown="다음 회의는 금요일이다.")


def _request(message: str, mode: str = "chat", **kwargs: object) -> AgentTurnRequest:
    return AgentTurnRequest(message=message, workspace_id="workspace-1", user_id="user-1", mode=mode, **kwargs)  # type: ignore[arg-type]


def _use_case(route: AgentTurnRoute):
    query = FakeQueryUseCase()
    replier = RecordingConversationReplier("대화 답변")
    starter = RecordingAgentRunStarter()
    editor = RecordingMarkdownEditor(
        MarkdownEditResult(
            edit=MarkdownEditOperation(
                operation="replace",
                target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=2),
                summary="요약",
                replacement_markdown="요약본",
            )
        )
    )
    router = FixedRouter(route)
    use_case = HandleAgentTurnUseCase(
        router=router,
        query_use_case=query,  # type: ignore[arg-type]
        markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
        markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
        agent_run_starter=starter,  # type: ignore[arg-type]
        conversation_replier=replier,
        reference_reader=FakeReader(),  # type: ignore[arg-type]
    )
    return use_case, query, replier, starter, editor, router


def _pinned_ids(query: FakeQueryUseCase) -> list[str]:
    return [snippet.source_document_id for snippet in query.kwargs[0]["pinned_evidence"]]  # type: ignore[union-attr]


# 요청 계약


def test_schema_accepts_chat_mode_and_rejects_others() -> None:
    base = {"message": "질문", "provider": "openai", "model": "gpt-5-nano"}

    assert AgentTurnRequestBody.model_validate({**base, "mode": "chat"}).to_domain().mode == "chat"
    with pytest.raises(ValueError):
        AgentTurnRequestBody.model_validate({**base, "mode": "wiki"})


def test_router_payload_tells_the_chat_tab_and_prompt_documents_it() -> None:
    assert route_payload(_request("질문"))["mode"] == "chat"
    prompt = DEFAULT_AGENT_TURN_ROUTER_PROMPT.read_text(encoding="utf-8")
    assert "Chat mode:" in prompt
    assert "attachments can only be chosen in the task tab" not in prompt


# 채팅 탭: 수정 동작이 일어나지 않는다


@pytest.mark.parametrize("route", CHANGING_ROUTES, ids=lambda route: f"{route.action}-{route.notice_kind}")
@pytest.mark.parametrize("context", [{}, ATTACHED, OPEN_DOCUMENT, {**ATTACHED, **OPEN_DOCUMENT}], ids=["none", "attached", "open", "both"])
def test_chat_tab_never_runs_a_change(route: AgentTurnRoute, context: dict[str, object]) -> None:
    use_case, query, replier, starter, editor, _ = _use_case(route)

    result = use_case.execute(_request("오타 고쳐 줘", folder_id="folder-1", **context))
    payload = _to_response(result).model_dump(mode="json")

    assert (result.action, result.route.notice_kind, result.message) == ("clarify", "task_tab", CLARIFY_TASK_TAB_MESSAGE)
    assert (result.edit, result.generated_markdown, result.run_id) == (None, None, None)
    assert (starter.requests, editor.requests, query.questions, replier.requests) == ([], [], [], [])
    assert (result.route.attached_edit_targets, result.route.include_active_markdown) == ((), False)
    assert payload["action"] == "clarify"


def test_chat_tab_router_turns_a_change_into_guidance_without_retry() -> None:
    client = SequenceJsonClient([route_response("markdown_edit")])

    route = ChatCompletionsTurnRouter(client, "system").route(_request("오타 고쳐 줘", **OPEN_DOCUMENT))  # type: ignore[arg-type]

    assert (route.action, route.notice_kind) == ("clarify", "task_tab")
    assert len(client.calls) == 1
    assert _route_failures(route, _request("오타 고쳐 줘", **OPEN_DOCUMENT)) == []


def test_chat_tab_ignores_skills() -> None:
    use_case, _, _, _, _, router = _use_case(ANSWER)

    use_case.execute(_request("이 문서의 베타 공개일이 언제야?", skill_mode="auto", **OPEN_DOCUMENT))

    assert router.requests[-1].skill_mode == "off"


# 채팅 탭: 첨부와 열린 문서를 근거로 답한다


def test_chat_tab_answers_from_attachment_and_open_document() -> None:
    use_case, query, _, _, editor, _ = _use_case(ANSWER)

    result = use_case.execute(_request("다음 회의가 언제야?", **ATTACHED, **OPEN_DOCUMENT))

    assert result.action == "chat_answer"
    assert _pinned_ids(query) == ["open-doc", "doc-1"]
    assert editor.requests == []


def test_chat_tab_answers_from_open_document_without_attachment() -> None:
    use_case, query, _, _, _, _ = _use_case(ANSWER)

    result = use_case.execute(_request("이 문서의 베타 공개일이 언제야?", **OPEN_DOCUMENT))

    assert result.action == "chat_answer"
    assert _pinned_ids(query) == ["open-doc"]


def test_chat_tab_answers_from_attachment_without_open_document() -> None:
    use_case, query, _, _, _, _ = _use_case(ANSWER)

    assert use_case.execute(_request("이 문서의 베타 공개일이 언제야?", **ATTACHED)).action == "chat_answer"
    assert _pinned_ids(query) == ["doc-1"]


def test_chat_tab_summary_of_this_document_uses_the_open_document_first() -> None:
    use_case, query, _, _, _, _ = _use_case(ANSWER)

    use_case.execute(_request("이 문서 요약해 줘", **ATTACHED, **OPEN_DOCUMENT))

    assert _pinned_ids(query)[0] == "open-doc"


def test_chat_tab_keeps_the_attachment_only_when_the_message_names_it() -> None:
    use_case, query, _, _, _, _ = _use_case(ANSWER)

    use_case.execute(_request("첨부 문서 요약해 줘", **ATTACHED, **OPEN_DOCUMENT))

    assert set(_pinned_ids(query)) == {"doc-1"}


def test_chat_tab_blank_note_is_not_evidence() -> None:
    use_case, query, _, _, _, _ = _use_case(ANSWER)

    result = use_case.execute(
        _request("회의가 언제야?", active_markdown_context=ActiveMarkdownContext(markdown="  \n"), document_id="blank", base_version=1)
    )

    assert result.action == "chat_answer"
    assert "pinned_evidence" not in query.kwargs[0]


def test_chat_tab_translation_reads_the_open_document() -> None:
    use_case, _, replier, _, _, _ = _use_case(REPLY)

    result = use_case.execute(_request("이 문서를 영어로 번역해 줘", **OPEN_DOCUMENT))

    assert (result.action, result.message) == ("conversation_reply", "대화 답변")
    assert replier.requests


@pytest.mark.parametrize("notice_kind", ["chat_tab", "details", None])
def test_chat_tab_redirect_clarify_becomes_an_answer(notice_kind: str | None) -> None:
    route = AgentTurnRoute(action="clarify", confidence=0.9, reason="question", notice_kind=notice_kind)  # type: ignore[arg-type]

    answered = task_mode_route(route, _request("질문", **ATTACHED))

    assert (answered.action, answered.retrieval_source, answered.notice_kind) == ("chat_answer", "workspace", None)


def test_chat_tab_keeps_reject() -> None:
    route = AgentTurnRoute(action="reject", confidence=0.9, reason="unsafe", notice_kind="policy")

    assert task_mode_route(route, _request("질문")) == route


def test_legacy_turn_keeps_its_routes() -> None:
    assert task_mode_route(EDIT, AgentTurnRequest(message="오타 고쳐 줘")) == EDIT


# 작업 탭: 질문에 답하지 않고 채팅 탭으로 돌려보낸다


QUESTION_CONTEXTS = [{}, ATTACHED, OPEN_DOCUMENT, {**ATTACHED, **OPEN_DOCUMENT}]


@pytest.mark.parametrize("route", [ANSWER, REPLY], ids=lambda route: route.action)
@pytest.mark.parametrize("context", QUESTION_CONTEXTS, ids=["none", "attached", "open", "both"])
@pytest.mark.parametrize("message", ["이 문서의 베타 공개일이 언제야?", "이 문서 요약해 줘", "이 문서 영어로 번역해 줘"])
def test_task_tab_sends_questions_to_the_chat_tab(route: AgentTurnRoute, context: dict[str, object], message: str) -> None:
    use_case, query, replier, starter, editor, _ = _use_case(route)

    result = use_case.execute(_request(message, mode="task", **context))
    payload = _to_response(result).model_dump(mode="json")

    assert (result.action, result.route.notice_kind) == ("clarify", "chat_tab")
    assert result.message == CLARIFY_TASK_MODE_QUESTION_MESSAGE
    assert "채팅 탭" in (result.message or "")
    assert payload["clarify_reason"] == "chat_tab_question"
    assert (query.questions, replier.requests, starter.requests, editor.requests) == ([], [], [], [])


def test_task_tab_chat_tab_clarify_with_attachment_stays_a_redirect() -> None:
    use_case, query, _, _, _, _ = _use_case(AgentTurnRoute(action="clarify", confidence=0.9, reason="q", notice_kind="chat_tab"))

    result = use_case.execute(_request("이 문서의 베타 공개일이 언제야?", mode="task", **ATTACHED))

    assert (result.action, result.message) == ("clarify", CLARIFY_TASK_MODE_QUESTION_MESSAGE)
    assert query.questions == []


# 자동 승인에서 요약·번역이 원문을 덮어쓰지 않는다


SUMMARY_EDIT = dict(document_operation="edit", edit_goal="shorten", edit_operation="replace", edit_destination="target", required_capabilities=["document-edit"])


@pytest.mark.parametrize("context", [OPEN_DOCUMENT, {**ATTACHED, **OPEN_DOCUMENT}], ids=["open", "both"])
@pytest.mark.parametrize("message", ["이 문서 요약해 줘", "이 문서 번역해 줘", "요약해 줘"])
def test_task_tab_summary_edit_retries_and_lands_on_the_redirect(context: dict[str, object], message: str) -> None:
    client = SequenceJsonClient([
        {**route_response("markdown_edit"), **SUMMARY_EDIT},
        route_response("chat_answer"),
    ])

    route = ChatCompletionsTurnRouter(client, "system").route(_request(message, mode="task", **context))  # type: ignore[arg-type]

    assert (route.action, route.document_operation, route.notice_kind) == ("clarify", "none", "chat_tab")
    assert len(client.calls) == 2


@pytest.mark.parametrize("context", [OPEN_DOCUMENT, {**ATTACHED, **OPEN_DOCUMENT}], ids=["open", "both"])
def test_task_tab_summary_is_never_an_edit(context: dict[str, object]) -> None:
    failures = _route_failures(replace(EDIT, edit_goal="shorten"), _request("이 문서 요약해 줘", mode="task", **context))

    assert any("explicitly ask to change" in failure for failure in failures)


@pytest.mark.parametrize("message", ["요약해서 고쳐 줘", "이 문서를 세 줄로 줄여서 고쳐 줘", "오타 고쳐 줘"])
def test_task_tab_keeps_explicit_edits(message: str) -> None:
    route = replace(EDIT, edit_goal="shorten" if "고쳐" in message and "오타" not in message else "cleanup")

    assert _route_failures(route, _request(message, mode="task", **OPEN_DOCUMENT)) == []
    assert task_mode_route(route, _request(message, mode="task", **OPEN_DOCUMENT)) == route


# 채팅 탭의 되묻기·안내는 코드로 알아본다(새 브라우저의 세션 분류)


def test_chat_tab_guidance_has_a_notice_code() -> None:
    use_case, _, _, _, _, _ = _use_case(EDIT)

    result = use_case.execute(_request("오타 고쳐 줘", **OPEN_DOCUMENT))

    assert result.clarify_reason == "chat_tab_notice"
    assert _to_response(result).model_dump(mode="json")["clarify_reason"] == "chat_tab_notice"


def test_chat_tab_translation_question_has_a_notice_code() -> None:
    use_case, _, _, _, _, _ = _use_case(REPLY)

    assert use_case.execute(_request("이 문서 번역해 줘", **OPEN_DOCUMENT)).clarify_reason == "chat_tab_notice"


def test_task_tab_clarify_has_no_chat_notice_code() -> None:
    use_case, _, _, _, _, _ = _use_case(AgentTurnRoute(action="clarify", confidence=0.9, reason="ask", notice_kind="web_disabled"))

    assert use_case.execute(_request("질문", mode="task")).clarify_reason is None
