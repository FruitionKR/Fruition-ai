"""문서를 연 채 첨부한 턴에서 "이 문서"는 열린 문서다. 첨부를 지목하면 그 첨부다. 문서를 열지 않은 턴은 그대로다."""

from pathlib import Path

import pytest

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.attached_edit_request import selects_attachment
from app.modules.agent.domain.entities import ActiveMarkdownContext, AgentTurnRoute
from app.modules.agent.domain.read_only_attachments import route_without_read_only_targets
from app.modules.agent.domain.reference_documents import (
    MAX_REFERENCE_ANSWER_CHARS,
    MAX_REFERENCE_EVIDENCE_CHARS,
    SelectedReferenceDocuments,
    reference_evidence_snippets,
)
from app.modules.markdown_edit.application.generate_markdown_document import GenerateMarkdownDocumentUseCase
from app.modules.markdown_edit.application.generate_markdown_edit import GenerateMarkdownEditUseCase
from app.modules.query.application.answer_context_formatter import AnswerContextFormatter
from app.modules.skill.domain.entities import SkillAuthoringReference
from tests.modules.agent.test_attached_document_edits import Editor, Reader, _document
from tests.modules.agent.test_attachment_turn_intent import (
    ANSWER_ROUTE,
    OPEN_DOCUMENT,
    _answer_raw,
    _answer_use_case,
    _create_raw,
    _edit_raw,
    _failures,
    _request,
)
from tests.modules.agent.test_handle_agent_turn import (
    FakeQueryUseCase,
    FixedRouter,
    RecordingConversationReplier,
    RecordingMarkdownEditor,
)

PROMPT = (Path(__file__).resolve().parents[3] / "prompts" / "agent_turn_router.system.md").read_text(encoding="utf-8")
TITLE = "매출 보고서"
WITHOUT_OPEN_DOCUMENT = {"active_markdown_context": None, "document_id": None, "base_version": None}
THIS_DOCUMENT_EDITS = [
    "이 문서의 '매출'을 고쳐 줘",
    "이 문서 고쳐 줘",
    "this document needs a fix, please fix it",
    "첨부 내용대로 이 문서를 고쳐 줘",
    "첨부 문서 참고해서 이 문서를 고쳐 줘",
    "이 문서의 오타를 모두 고쳐 줘",
]
ATTACHMENT_SELECTIONS = [
    "첨부 문서 고쳐 줘",
    "첨부한 문서 고쳐 줘",
    "첨부한 이 문서 고쳐 줘",
    "이 문서 말고 첨부 문서 고쳐 줘",
    f"{TITLE} 고쳐 줘",
    "두 번째 문서 고쳐 줘",
    "이 문서 둘 다 고쳐 줘",
]


def _opened(message: str, *attachments: str, **values: object):
    return _request(message, *(attachments or ("doc-1",)), reference_document_titles=(TITLE,), **values)


def _pinned_ids(use_case_query: FakeQueryUseCase) -> list[str]:
    return list(dict.fromkeys(snippet.source_document_id for snippet in use_case_query.kwargs[0]["pinned_evidence"]))


@pytest.mark.parametrize("message", THIS_DOCUMENT_EDITS)
def test_this_document_cannot_target_an_attachment(message: str) -> None:
    raw = _edit_raw("workspace_workflow", attached_edit_targets=[1], edit_goal="other")

    assert any("must name an attachment" in failure for failure in _failures(raw, _opened(message)))


@pytest.mark.parametrize("message", THIS_DOCUMENT_EDITS)
def test_this_document_edit_stays_on_open_document(message: str) -> None:
    assert _failures(_edit_raw(edit_goal="other"), _opened(message)) == []


@pytest.mark.parametrize("message", THIS_DOCUMENT_EDITS)
def test_this_document_cannot_ask_which_attachment(message: str) -> None:
    raw = _answer_raw()
    raw.update(
        action="clarify",
        retrieval_source="none",
        notice_kind="attachment_target",
    )

    assert any("attachment_target" in failure and "active Markdown" in failure for failure in _failures(raw, _opened(message)))


@pytest.mark.parametrize("message", ATTACHMENT_SELECTIONS)
def test_selecting_an_attachment_keeps_attachment_target(message: str) -> None:
    raw = _edit_raw("workspace_workflow", attached_edit_targets=[1], edit_goal="other")

    assert _failures(raw, _opened(message, "doc-1", "doc-2")) == []


def test_asking_which_attachment_stays_for_a_selection_of_attachments() -> None:
    raw = _answer_raw()
    raw.update(action="clarify", retrieval_source="none", notice_kind="attachment_target")

    assert _failures(raw, _opened("첨부 문서 고쳐 줘", "doc-1", "doc-2")) == []


@pytest.mark.parametrize("message", ["이 문서 요약해 줘", "이 문서 세 줄로 요약해 줘", "이 문서가 언제 작성됐어?"])
def test_answer_about_this_document_pins_open_document_first_and_keeps_attachment(message: str) -> None:
    use_case, query = _answer_use_case(ANSWER_ROUTE, Reader(_document("doc-1", "첨부")))

    use_case.execute(_opened(message))

    pinned = query.kwargs[0]["pinned_evidence"]
    assert _pinned_ids(query) == ["open-doc", "doc-1"]
    assert "열린 문서 본문" in pinned[0].text


@pytest.mark.parametrize(
    "message",
    ["첨부 문서 요약해 줘", "첨부한 문서 요약해 줘", "첨부한 이 문서 요약해 줘", "이 문서 말고 첨부 문서 요약해 줘", f"{TITLE} 요약해 줘", "두 번째 문서 요약해 줘", "요약해 줘"],
)
def test_answer_selecting_an_attachment_pins_only_attachment(message: str) -> None:
    use_case, query = _answer_use_case(ANSWER_ROUTE, Reader(_document("doc-1", "첨부")))

    use_case.execute(_opened(message))

    assert _pinned_ids(query) == ["doc-1"]


def test_answer_comparing_this_document_with_attachment_pins_both() -> None:
    use_case, query = _answer_use_case(ANSWER_ROUTE, Reader(_document("doc-1", "첨부")))

    use_case.execute(_opened("이 문서와 첨부 문서를 비교해 줘"))

    assert _pinned_ids(query) == ["open-doc", "doc-1"]


def test_answer_does_not_pin_open_document_twice_when_it_is_also_attached() -> None:
    use_case, query = _answer_use_case(ANSWER_ROUTE, Reader(_document("doc-1", "같은 문서"), _document("doc-2", "첨부")))

    use_case.execute(_request("이 문서 요약해 줘", "doc-1", "doc-2", document_id="doc-1"))

    pinned = query.kwargs[0]["pinned_evidence"]
    assert _pinned_ids(query) == ["doc-1", "doc-2"]
    assert sum(snippet.source_document_id == "doc-1" for snippet in pinned) == 1
    assert "열린 문서 본문" in pinned[0].text


def test_long_open_document_leaves_room_for_every_attachment() -> None:
    reader = Reader(_document("doc-1", "첨부 하나" * 500), _document("doc-2", "첨부 둘" * 500))
    use_case, query = _answer_use_case(ANSWER_ROUTE, reader)
    long_open = ActiveMarkdownContext(markdown="가" * (MAX_REFERENCE_ANSWER_CHARS + 10))

    result = use_case.execute(_request("이 문서 요약해 줘", "doc-1", "doc-2", active_markdown_context=long_open))

    assert result.action == "chat_answer"
    assert _pinned_ids(query) == ["open-doc", "doc-1", "doc-2"]
    assert all(call["max_chars"] >= MAX_REFERENCE_EVIDENCE_CHARS for call in reader.body_calls)


def test_answer_input_tells_open_document_from_attachment() -> None:
    selected = SelectedReferenceDocuments(
        documents=(
            SkillAuthoringReference(id="open-doc", name="", markdown="열린 본문"),
            SkillAuthoringReference(id="doc-1", name=TITLE, markdown="첨부 본문"),
        ),
        open_document_id="open-doc",
    )

    context = AnswerContextFormatter().format("이 문서 요약해 줘", [], [], list(reference_evidence_snippets(selected)))

    open_section, attachment_section = context.split("## Evidence 2")
    assert "- source: the document open in the editor" in open_section
    assert "attached by the user" not in open_section.split("## Evidence 1")[1]
    assert "- source: document attached by the user" in attachment_section
    assert "open in the editor" in context.split("# Evidence Snippets By Relevance")[0]


def test_answer_input_without_open_document_has_no_open_document_policy() -> None:
    selected = SelectedReferenceDocuments(documents=(SkillAuthoringReference(id="doc-1", name=TITLE, markdown="첨부 본문"),))

    context = AnswerContextFormatter().format("이 문서 요약해 줘", [], [], list(reference_evidence_snippets(selected)))

    assert "open in the editor" not in context


def test_reply_about_this_document_gets_only_open_document() -> None:
    replier = RecordingConversationReplier("번역")
    reader = Reader(_document("doc-1", "첨부"))
    use_case, _ = _answer_use_case(
        AgentTurnRoute(action="conversation_reply", confidence=1.0, reason="번역"),
        reader,
        conversation_replier=replier,
    )

    use_case.execute(_opened("이 문서를 영어로 번역해 줘"))

    context = replier.requests[0].conversation_context.reference_context
    assert [document["markdown"] for document in context["reference_documents"]] == [OPEN_DOCUMENT.markdown]
    assert reader.body_calls == []


def test_translation_of_this_document_without_language_asks_first() -> None:
    use_case, query = _answer_use_case(
        AgentTurnRoute(action="conversation_reply", confidence=1.0, reason="번역"),
        Reader(_document("doc-1", "첨부")),
        conversation_replier=RecordingConversationReplier("번역"),
    )

    result = use_case.execute(_opened("이 문서 번역해 줘"))

    assert result.action == "clarify"
    assert query.kwargs == []


CREATE_ROUTE = AgentTurnRoute(
    action="markdown_create",
    confidence=1.0,
    reason="저장",
    document_operation="create",
    required_capabilities=("document-create",),
    edit_goal="create_from_chat",
)


def _create_use_case(reader: Reader) -> tuple[HandleAgentTurnUseCase, RecordingMarkdownEditor]:
    editor = RecordingMarkdownEditor(AssertionError("수정하지 않는다"))
    return (
        HandleAgentTurnUseCase(
            router=FixedRouter(CREATE_ROUTE),
            query_use_case=FakeQueryUseCase(),  # type: ignore[arg-type]
            markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
            markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
            reference_reader=reader,  # type: ignore[arg-type]
        ),
        editor,
    )


@pytest.mark.parametrize("message", ["이 문서 요약해서 저장해 줘", "이 문서를 요약해서 새 문서로 만들어 줘"])
def test_saving_a_summary_of_this_document_uses_only_open_document(message: str) -> None:
    reader = Reader(_document("doc-1", "첨부"))
    use_case, editor = _create_use_case(reader)

    use_case.execute(_opened(message))

    context = editor.create_requests[0].reference_context
    assert context["source_document"]["markdown"] == OPEN_DOCUMENT.markdown
    assert "reference_documents" not in context
    assert reader.body_calls == []


def test_saving_this_document_summary_is_not_a_contract_failure() -> None:
    assert _failures(_create_raw(), _opened("이 문서 요약해서 저장해 줘")) == []


@pytest.mark.parametrize("message", ["첨부 문서 요약해서 저장해 줘", "요약해서 저장해 줘", "첨부 내용을 반영해서 이 문서를 요약해서 저장해 줘"])
def test_saving_a_summary_that_names_attachment_or_nothing_keeps_attachment(message: str) -> None:
    use_case, editor = _create_use_case(Reader(_document("doc-1", "첨부")))

    use_case.execute(_opened(message))

    context = editor.create_requests[0].reference_context
    assert [document["document_id"] for document in context["reference_documents"]] == ["doc-1"]
    assert "source_document" not in context


def test_edit_of_this_document_with_attachment_as_source_keeps_attachment_as_evidence() -> None:
    reader = Reader(_document("doc-1", "첨부"))
    use_case = HandleAgentTurnUseCase(
        router=FixedRouter(
            AgentTurnRoute(
                action="markdown_edit",
                confidence=1.0,
                reason="수정",
                document_operation="edit",
                required_capabilities=("document-edit",),
                edit_goal="other",
                edit_operation="replace",
                edit_destination="target",
            )
        ),
        query_use_case=FakeQueryUseCase(),  # type: ignore[arg-type]
        markdown_edit_use_case=GenerateMarkdownEditUseCase(editor := Editor()),
        markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
        reference_reader=reader,  # type: ignore[arg-type]
    )

    result = use_case.execute(_opened("첨부 내용대로 이 문서를 고쳐 줘"))

    assert result.action == "markdown_edit"
    assert editor.requests[0].markdown == OPEN_DOCUMENT.markdown
    assert [document["document_id"] for document in editor.requests[0].reference_context["reference_documents"]] == ["doc-1"]


# 문서를 열지 않은 턴은 이번 결정의 대상이 아니다.
def test_without_open_document_this_document_still_means_the_attachment() -> None:
    raw = _edit_raw("workspace_workflow", attached_edit_targets=[1], edit_goal="cleanup")
    request = _opened("이 문서의 오타를 고쳐 줘", **WITHOUT_OPEN_DOCUMENT)

    assert _failures(raw, request) == []


def test_without_open_document_answer_pins_the_attachment() -> None:
    use_case, query = _answer_use_case(ANSWER_ROUTE, Reader(_document("doc-1", "첨부")))

    use_case.execute(_opened("이 문서 요약해 줘", **WITHOUT_OPEN_DOCUMENT))

    assert _pinned_ids(query) == ["doc-1"]


def test_without_open_document_summary_save_uses_the_attachment() -> None:
    use_case, editor = _create_use_case(Reader(_document("doc-1", "첨부")))

    use_case.execute(_opened("이 문서 요약해서 저장해 줘", **WITHOUT_OPEN_DOCUMENT))

    context = editor.create_requests[0].reference_context
    assert [document["document_id"] for document in context["reference_documents"]] == ["doc-1"]
    assert "source_document" not in context


def test_read_only_turn_keeps_this_document_as_the_models_choice() -> None:
    """보기 권한으로 연 문서의 턴에는 열린 문서 정보가 없다. 모델이 고른 대상을 코드가 옮기지 않는다."""
    route = AgentTurnRoute(
        action="workspace_workflow",
        confidence=1.0,
        reason="수정",
        document_operation="edit",
        persist=True,
        required_capabilities=("document-edit",),
        edit_goal="other",
        edit_operation="replace",
        edit_destination="target",
        attached_edit_targets=(2,),
    )
    request = _request(
        "이 문서 고쳐 줘",
        "read-only",
        "editable",
        reference_document_titles=("보기전용", "편집문서"),
        reference_document_editable=(False, True),
        **WITHOUT_OPEN_DOCUMENT,
    )

    assert route_without_read_only_targets(route, request) == route


def test_selects_attachment_counts_this_document_as_the_open_document() -> None:
    assert selects_attachment("첨부 내용대로 이 문서를 고쳐 줘") is False
    assert selects_attachment("이 문서의 항목을 모두 고쳐 줘") is False
    assert selects_attachment("이 문서들 모두 고쳐 줘") is True


def test_prompt_states_this_document_means_the_active_markdown() -> None:
    assert "with active Markdown" in PROMPT
    for message in ("이 문서 요약해 줘", "이 문서의 '매출'을 고쳐 줘", "이 문서 요약해서 저장해 줘", "첨부 내용대로 이 문서를 고쳐 줘"):
        assert message in PROMPT
