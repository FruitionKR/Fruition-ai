"""첨부 문서 자체를 고치는 요청의 라우터 계약(route.attached_edit_targets)."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.pipeline_control import PipelineRunCancelledError
from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnResult, AgentTurnRoute
from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    ChatCompletionsTurnRouter,
    _normalize_route,
    _route_failures,
    load_agent_turn_router_prompt,
    route_payload,
)
from app.modules.agent.infrastructure.jev_turn_router import JevTurnRouter
from app.modules.agent.interfaces.http.responses import _to_response
from app.modules.markdown_edit.application.generate_markdown_document import GenerateMarkdownDocumentUseCase
from app.modules.markdown_edit.application.generate_markdown_edit import GenerateMarkdownEditUseCase
from app.modules.markdown_edit.domain.entities import MarkdownEditResult
from app.modules.skill.domain.entities import SkillAuthoringReference
from app.modules.skill.domain.exceptions import ReferenceDocumentTooLargeError, SkillRequestRejectedError
from app.modules.skill.infrastructure.backend_skill_reference_reader import BackendSkillReferenceReader
from evaluate_agent_turn_router import (
    DEFAULT_DATASET,
    evaluate_cases,
    load_cases,
    request_from_case,
)
from tests.modules.agent.test_handle_agent_turn import FakeQueryUseCase, FixedRouter, RecordingMarkdownEditor


def _request(*ids: str, titles: tuple[str, ...] = (), **values: object) -> AgentTurnRequest:
    return AgentTurnRequest(
        message="첨부 문서를 고쳐줘", reference_document_ids=ids, reference_document_titles=titles, **values
    )


def _raw(**values: object) -> dict[str, object]:
    return {
        "action": "workspace_workflow",
        "confidence": 0.9,
        "reason": "첨부 수정",
        "retrieval_source": "none",
        "document_operation": "edit",
        "persist": True,
        "required_capabilities": ["document-edit"],
        "edit_goal": "style_change",
        "edit_operation": "replace",
        "edit_destination": "target",
        "selected_skill_id": None,
        "skill_candidates": [],
        "notice_kind": None,
        "attached_edit_targets": [1],
        **values,
    }


def _failures(raw: dict[str, object], request: AgentTurnRequest) -> list[str]:
    route, failures = _normalize_route(raw)
    return [*failures, *_route_failures(route, request)]


def test_payload_carries_count_and_titles_without_ids_or_body() -> None:
    payload = route_payload(_request("doc-1", "doc-2", titles=("회의록", "기획서")))

    assert payload["reference_documents"] == {"count": 2, "titles": ["회의록", "기획서"]}
    assert "doc-1" not in json.dumps(payload, ensure_ascii=False)


def test_payload_keeps_titles_aligned_when_titles_were_not_read() -> None:
    payload = route_payload(_request("doc-1", "doc-2", "doc-1"))

    assert payload["reference_documents"] == {"count": 2, "titles": ["", ""]}


def test_route_without_attached_targets_defaults_to_empty() -> None:
    raw = _raw()
    del raw["attached_edit_targets"]

    route, failures = _normalize_route(raw)

    assert failures == []
    assert route.attached_edit_targets == ()


@pytest.mark.parametrize(
    "value", ["1", [0.5], [True], [1, 1], [-1], [0]], ids=["string", "float", "bool", "duplicate", "negative", "zero"]
)
def test_invalid_attached_targets_shape_is_contract_failure(value: object) -> None:
    assert _failures(_raw(attached_edit_targets=value), _request("doc-1"))


def test_valid_replace_and_document_end_targets_pass() -> None:
    request = _request("doc-1", "doc-2")

    assert _failures(_raw(attached_edit_targets=[1, 2]), request) == []
    assert _failures(_raw(edit_operation="insert_after", edit_destination="document_end", edit_goal="other"), request) == []


def test_target_out_of_range_is_contract_failure() -> None:
    assert any("attached_edit_targets" in failure for failure in _failures(_raw(attached_edit_targets=[2]), _request("doc-1")))


def test_targets_require_save_workflow_edit() -> None:
    request = _request("doc-1")
    markdown_edit = _raw(action="markdown_edit", persist=False)

    assert any("attached_edit_targets" in failure for failure in _failures(markdown_edit, request))


def test_targets_reject_folder_capability() -> None:
    raw = _raw(required_capabilities=["document-edit", "folder-organize"])

    assert any("attached_edit_targets" in failure for failure in _failures(raw, _request("doc-1")))


def test_targets_reject_section_insert() -> None:
    raw = _raw(edit_operation="insert_after", edit_destination="target", edit_goal="other")

    assert any("attached_edit_targets" in failure for failure in _failures(raw, _request("doc-1")))


def _attachment_clarify(**values: object) -> dict[str, object]:
    return _raw(
        action="clarify",
        persist=False,
        document_operation="none",
        required_capabilities=[],
        edit_goal=None,
        edit_operation=None,
        edit_destination=None,
        attached_edit_targets=[],
        notice_kind="attachment_target",
        **values,
    )


def test_attachment_target_notice_is_valid_outside_task_mode_with_attachments() -> None:
    assert _failures(_attachment_clarify(), _request("doc-1", "doc-2")) == []
    assert _failures(_attachment_clarify(), _request("doc-1", "doc-2", mode="task")) == []


def test_attachment_target_notice_requires_attachments() -> None:
    assert any("attachment_target" in failure for failure in _failures(_attachment_clarify(), _request()))


class SequenceClient:
    def __init__(self, *responses: dict[str, object]) -> None:
        self.responses = list(responses)
        self.user_prompts: list[str] = []

    def complete_json(self, _system: str, user: str) -> dict[str, object]:
        self.user_prompts.append(user)
        return self.responses.pop(0)


def test_router_retries_out_of_range_target_then_fails() -> None:
    client = SequenceClient(_raw(attached_edit_targets=[5]), _raw(attached_edit_targets=[5]))

    with pytest.raises(AgentTurnRouteContractError):
        ChatCompletionsTurnRouter(client, "system").route(_request("doc-1"))  # type: ignore[arg-type]

    assert len(client.user_prompts) == 2


_SUMMARY_ROUTE = {
    "action": "chat_answer",
    "confidence": 0.9,
    "reason": "첨부 요약",
    "retrieval_source": "workspace",
    "document_operation": "none",
    "persist": False,
    "required_capabilities": [],
    "edit_goal": None,
    "edit_operation": None,
    "edit_destination": None,
    "selected_skill_id": None,
    "skill_candidates": [],
    "notice_kind": None,
    "attached_edit_targets": [],
}


def _guard_failures(message: str, edit_goal: str) -> list[str]:
    request = replace(_request("doc-1"), message=message)
    failures = _failures(_raw(edit_goal=edit_goal), request)
    return [failure for failure in failures if "explicitly" in failure]


@pytest.mark.parametrize(
    ("message", "edit_goal"),
    [
        ("이 문서 세 줄로 요약해 줘", "shorten"),
        ("이 문서 세 줄로 요약해 줘", "other"),
        ("요약해서 새 문서로 만들어 줘", "shorten"),
        ("요약해서 저장해 줘", "shorten"),
        ("원문은 고치지 말고 세 줄로 요약해 줘", "shorten"),
        ("수정 사항을 요약해 줘", "shorten"),
        ("변경 내용을 간추려 줘", "shorten"),
        ("세 줄로 줄여서 요약해 줘", "shorten"),
        ("첨부한 문서를 영어로 번역해 줘", "translate"),
        ("Summarize this document in three lines", "shorten"),
        ("Summarize it and don't change the original", "shorten"),
        ("Translate this into Korean", "translate"),
        # 목록에 없는 말로 결과물을 요구해도 막는다. 틀리면 원문을 고치지 않는 쪽으로 틀린다.
        ("이 문서 요약 좀", "other"),
        ("요약본을 만들어 줘", "other"),
        ("Give me a summary of the attachment", "other"),
        # 요약·번역 섹션을 대상으로 다시 요약·번역을 요구하는 말이다.
        ("요약 섹션을 영어로 번역해 줘", "other"),
        ("Summarize the summary section", "other"),
        ("Summarize section 3", "other"),
        ("Translate section 3", "other"),
        # 요약 동사의 목적어인 영어 명사는 수정 요청이 아니다.
        ("Summarize the latest update in this doc", "shorten"),
        ("Summarize what to fix", "shorten"),
        ("Summarize what I need to fix", "shorten"),
        ("Summarize the add-on features", "shorten"),
        ("Summarize the release notes and update log", "shorten"),
        ("Summarize the add, remove, and edit buttons", "shorten"),
        ("Translate the change log", "translate"),
    ],
)
def test_summary_or_translation_without_change_verb_cannot_target_attachment(message: str, edit_goal: str) -> None:
    assert _guard_failures(message, edit_goal)


@pytest.mark.parametrize(
    ("message", "edit_goal"),
    [
        ("이 문서를 세 줄로 줄여서 고쳐 줘", "shorten"),
        ("첨부한 회의록을 더 짧게 줄여줘", "shorten"),
        ("이 문서를 더 짧게 해 줘", "shorten"),
        ("요약해서 문서 내용을 바꿔 줘", "shorten"),
        ("요약한 내용으로 원문을 덮어써 줘", "shorten"),
        ("문서 맨 앞에 요약을 넣어 줘", "other"),
        ("요약 부분을 지워 줘", "other"),
        ("영어로 번역해서 문서에 반영해 줘", "translate"),
        ("Shorten the attached document", "shorten"),
        ("Translate it and apply the result to the document", "translate"),
        # 요약·번역이 아니면 표현을 제한하지 않는다. 어떤 언어든 라우터 판단을 그대로 쓴다.
        ("첨부한 문서를 더 부드러운 말투로", "style_change"),
        ("添付文書の誤字を直して", "cleanup"),
        ("Make the attachment more polite", "style_change"),
        # "요약 섹션"처럼 문서의 일부를 가리키는 말은 결과물 요구가 아니다.
        ("첨부한 문서의 요약 섹션을 더 자세히 써 줘", "other"),
        ("번역 섹션을 보강해 줘", "other"),
        ("Expand the summary section", "other"),
        ("Rework the translation part", "other"),
        ("요약 섹션을 세 줄로 줄여 줘", "shorten"),
        # 영어 수정 동사는 명령형 자리에 있으면 수정 요청이다.
        ("Summarize this and apply it to the document", "shorten"),
        ("Summarize this. Then update the document", "shorten"),
        ("Please fix the summary", "other"),
        ("Could you update the translation?", "translate"),
        ("Summarize the changes, add them to the top", "shorten"),
        ("Update the summary", "other"),
    ],
)
def test_explicit_change_requests_keep_attachment_target(message: str, edit_goal: str) -> None:
    assert _guard_failures(message, edit_goal) == []


def test_router_retries_summary_misrouted_as_attachment_edit_then_answers() -> None:
    client = SequenceClient(_raw(edit_goal="shorten"), _SUMMARY_ROUTE)
    request = replace(_request("doc-1", mode="task"), message="이 문서 세 줄로 요약해 줘")

    route = ChatCompletionsTurnRouter(client, "system").route(request)  # type: ignore[arg-type]

    assert (route.action, route.attached_edit_targets) == ("chat_answer", ())
    retry = json.loads(client.user_prompts[1])
    assert any("explicitly" in failure for failure in retry["contract_failures"])


def test_router_never_returns_attachment_edit_for_summary_even_when_model_insists() -> None:
    client = SequenceClient(_raw(edit_goal="shorten"), _raw(edit_goal="shorten"))
    request = replace(_request("doc-1"), message="이 문서 세 줄로 요약해 줘")

    with pytest.raises(AgentTurnRouteContractError):
        ChatCompletionsTurnRouter(client, "system").route(request)  # type: ignore[arg-type]


def test_jev_sends_attachment_turns_to_llm_router() -> None:
    class Fallback:
        requests: list[AgentTurnRequest] = []

        def route(self, request: AgentTurnRequest) -> AgentTurnRoute:
            self.requests.append(request)
            return AgentTurnRoute(action="conversation_reply", confidence=1.0, reason="llm")

    class ExplodingJev:
        def choose(self, *_args: object) -> dict[str, object]:
            raise AssertionError("첨부 턴은 Jev에 보내지 않는다")

    request = _request("doc-1")
    fallback = Fallback()

    route = JevTurnRouter(ExplodingJev(), fallback, "system").route(request)  # type: ignore[arg-type]

    assert route.reason == "llm"
    assert fallback.requests == [request]


def test_prompt_states_attachment_edit_and_evidence_rules() -> None:
    prompt = load_agent_turn_router_prompt()

    assert "attached_edit_targets" in prompt
    assert "attachment_target" in prompt
    assert "Attached documents are evidence and are never an edit target" not in prompt
    assert "이 문서 세 줄로 요약해 줘" in prompt
    assert "이 문서를 세 줄로 줄여서 고쳐 줘" in prompt


def _attachment_cases() -> list[dict[str, object]]:
    return [case for case in load_cases(DEFAULT_DATASET) if case["id"].startswith("attachment-")]


def test_eval_has_attachment_edit_and_evidence_contrast_cases() -> None:
    cases = _attachment_cases()
    targets = {tuple(case["expected"].get("attached_edit_targets", [])) for case in cases}

    assert len(cases) >= 8
    assert {(), (1,), (1, 2)} <= targets
    assert any(case["expected"].get("notice_kind") == "attachment_target" for case in cases)


def test_eval_separates_summary_translation_answers_from_attachment_edits() -> None:
    expected = {case["id"]: case["expected"] for case in _attachment_cases()}

    for case_id in ("attachment-summary-this-document", "attachment-summary-keep-original", "attachment-translate-answer"):
        assert expected[case_id]["document_operation"] == "none"
        assert not expected[case_id].get("attached_edit_targets")
    assert expected["attachment-summary-new-document"]["document_operation"] == "create"
    assert expected["attachment-shorten-and-change"]["attached_edit_targets"] == [1]
    assert expected["attachment-translate-apply"]["attached_edit_targets"] == [1]


def test_eval_request_carries_attachment_titles() -> None:
    case = next(case for case in _attachment_cases() if case["expected"].get("attached_edit_targets") == [1])

    request = request_from_case(case)

    assert len(request.reference_document_ids) == len(request.reference_document_titles) >= 1


def test_eval_compares_attached_targets_and_notice() -> None:
    case = next(case for case in _attachment_cases() if case["expected"].get("attached_edit_targets") == [1])
    expected = case["expected"]

    class Router:
        def __init__(self, targets: tuple[int, ...]) -> None:
            self.targets = targets

        def route(self, _request: object) -> AgentTurnRoute:
            return AgentTurnRoute(
                action=expected["action"],
                confidence=1.0,
                reason="평가",
                retrieval_source=expected["retrieval_source"],
                document_operation=expected["document_operation"],
                persist=expected["persist"],
                required_capabilities=tuple(expected["required_capabilities"]),
                edit_goal=expected["edit_goal"],
                edit_operation=expected["edit_operation"],
                edit_destination=expected["edit_destination"],
                attached_edit_targets=self.targets,
            )

    assert evaluate_cases(Router((1,)), [case])["summary"]["route_correct"] == 1
    assert evaluate_cases(Router(()), [case])["summary"]["route_correct"] == 0


def test_existing_cases_keep_working_without_attachment_fields() -> None:
    assert Path(DEFAULT_DATASET).exists()
    for case in load_cases(DEFAULT_DATASET):
        assert request_from_case(case).message


class _TitleReader:
    def __init__(self, documents: dict[str, SkillAuthoringReference | Exception]) -> None:
        self.documents = documents
        self.calls: list[dict[str, object]] = []

    def read(self, **kwargs: object) -> SkillAuthoringReference:
        self.calls.append(kwargs)
        document = self.documents[str(kwargs["document_id"])]
        if isinstance(document, Exception):
            raise document
        return document


def _use_case(route: AgentTurnRoute, reader: object) -> tuple[HandleAgentTurnUseCase, FixedRouter]:
    router = FixedRouter(route)
    editor = RecordingMarkdownEditor(MarkdownEditResult(edit=None))  # type: ignore[arg-type]
    return (
        HandleAgentTurnUseCase(
            router=router,
            query_use_case=FakeQueryUseCase(),  # type: ignore[arg-type]
            markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
            markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
            reference_reader=reader,  # type: ignore[arg-type]
        ),
        router,
    )


CLARIFY_ATTACHMENT_ROUTE = AgentTurnRoute(
    action="clarify", confidence=0.5, reason="모호함", notice_kind="attachment_target"
)


def _turn(*ids: str) -> AgentTurnRequest:
    return AgentTurnRequest(
        message="회의록을 고쳐줘", workspace_id="workspace-1", user_id="user-1", reference_document_ids=ids
    )


def test_titles_are_read_with_one_char_and_cleaned_before_routing() -> None:
    reader = _TitleReader(
        {
            "doc-1": SkillAuthoringReference(id="doc-1", name="회의\n록​", markdown="본"),
            "doc-2": SkillAuthoringReference(id="doc-2", name="기획서", markdown="본"),
        }
    )
    use_case, router = _use_case(CLARIFY_ATTACHMENT_ROUTE, reader)

    use_case.execute(_turn("doc-1", "doc-2", "doc-1"))

    assert router.requests[0].reference_document_titles == ("회의 록", "기획서")
    assert [(call["document_id"], call["max_chars"]) for call in reader.calls] == [("doc-1", 1), ("doc-2", 1)]


@pytest.mark.parametrize(
    "error",
    [ReferenceDocumentTooLargeError(), SkillRequestRejectedError("invalid_reference", "x"), RuntimeError("down")],
    ids=["413", "rejected", "service"],
)
def test_unreadable_title_stays_empty_so_numbers_stay_aligned(error: Exception) -> None:
    reader = _TitleReader({"doc-1": error, "doc-2": SkillAuthoringReference(id="doc-2", name="기획서", markdown="본")})
    use_case, router = _use_case(CLARIFY_ATTACHMENT_ROUTE, reader)

    use_case.execute(_turn("doc-1", "doc-2"))

    assert router.requests[0].reference_document_titles == ("", "기획서")


def test_title_read_does_not_swallow_cancellation() -> None:
    reader = _TitleReader({"doc-1": PipelineRunCancelledError("cancelled")})
    use_case, router = _use_case(CLARIFY_ATTACHMENT_ROUTE, reader)

    with pytest.raises(PipelineRunCancelledError):
        use_case.execute(_turn("doc-1"))

    assert router.requests == []


def test_ambiguous_attachment_lists_titles_in_order_with_reason() -> None:
    reader = _TitleReader(
        {
            "doc-1": SkillAuthoringReference(id="doc-1", name="회의록", markdown="본"),
            "doc-2": SkillAuthoringReference(id="doc-2", name="", markdown="본"),
            "doc-3": SkillAuthoringReference(id="doc-3", name="회의록", markdown="본"),
        }
    )
    use_case, _ = _use_case(CLARIFY_ATTACHMENT_ROUTE, reader)

    result = use_case.execute(_turn("doc-1", "doc-2", "doc-3"))

    assert result.action == "clarify"
    assert result.clarify_reason == "attachment_target_required"
    assert result.message.index("1. 회의록") < result.message.index("2. 제목 없는 문서") < result.message.index("3. 회의록")


def test_attachment_clarify_response_serializes_reason_and_targets() -> None:
    result = AgentTurnResult(
        action="clarify",
        route=replace(CLARIFY_ATTACHMENT_ROUTE, attached_edit_targets=()),
        clarify_reason="attachment_target_required",
        message="골라 주세요",
    )

    response = _to_response(result)

    assert response.clarify_reason == "attachment_target_required"
    assert response.route.notice_kind == "attachment_target"
    assert response.route.attached_edit_targets == []


def test_reader_parses_edit_revision_and_can_edit_only_for_valid_values() -> None:
    def read(value: dict[str, object]) -> SkillAuthoringReference:
        body = {"document_role": "EDITABLE", "markdown": "본문", **value}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(body).encode()
        with patch(
            "app.modules.skill.infrastructure.backend_skill_reference_reader.urlopen", return_value=response
        ):
            return BackendSkillReferenceReader("http://document", "token").read(
                workspace_id="w", user_id="u", document_id="d"
            )

    current = read({"edit_revision": 7, "can_edit": True})
    assert (current.edit_revision, current.can_edit) == (7, True)
    old = read({})
    assert (old.edit_revision, old.can_edit) == (None, False)
    invalid = read({"edit_revision": True, "can_edit": "true"})
    assert (invalid.edit_revision, invalid.can_edit) == (None, False)
    assert read({"edit_revision": 0, "can_edit": True}).edit_revision is None
