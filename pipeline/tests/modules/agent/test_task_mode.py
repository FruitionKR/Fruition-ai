"""작업 탭(mode=task) 턴의 라우팅·실행 규칙."""

import json
import unittest

from pydantic import ValidationError

from app.modules.agent.application.handle_agent_turn import (
    CLARIFY_MARKDOWN_DOCUMENT_MESSAGE,
    CLARIFY_TASK_MODE_QUESTION_MESSAGE,
    REFERENCE_ALL_TOO_LONG_MESSAGE,
    REFERENCE_NOT_ACCESSIBLE_MESSAGE,
    REFERENCE_READ_FAILED_MESSAGE,
    HandleAgentTurnUseCase,
)
from app.modules.agent.domain.entities import ActiveMarkdownContext, AgentTurnRequest, AgentTurnRoute
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    DEFAULT_AGENT_TURN_ROUTER_PROMPT,
    ChatCompletionsTurnRouter,
    _route_failures,
    route_payload,
)
from app.modules.agent.infrastructure.jev_turn_router import route_options
from app.modules.agent.interfaces.http.responses import _to_response
from app.modules.agent.interfaces.http.schemas import AgentTurnRequestBody
from app.modules.markdown_edit.application.generate_markdown_document import (
    GenerateMarkdownDocumentUseCase,
)
from app.modules.markdown_edit.application.generate_markdown_edit import (
    GenerateMarkdownEditUseCase,
)
from app.modules.markdown_edit.domain.entities import (
    MarkdownEditOperation,
    MarkdownEditResult,
    MarkdownEditTarget,
)
from app.modules.skill.domain.entities import SkillAuthoringReference
from app.modules.skill.domain.exceptions import ReferenceDocumentTooLargeError, SkillRequestRejectedError
from tests.modules.agent.test_chat_completions_turn_router import SequenceJsonClient, route_response
from tests.modules.agent.test_handle_agent_turn import (
    FakeQueryUseCase,
    FixedRouter,
    RecordingAgentRunStarter,
    RecordingConversationReplier,
    RecordingMarkdownEditor,
)

TASK_EXCLUDED = ("chat_answer", "conversation_reply")


def task_request(message: str = "회의록 내용이 뭐야?", **kwargs: object) -> AgentTurnRequest:
    return AgentTurnRequest(
        message=message,
        workspace_id="workspace-1",
        user_id="user-1",
        mode="task",
        **kwargs,  # type: ignore[arg-type]
    )


def build_use_case(route: AgentTurnRoute) -> tuple[
    HandleAgentTurnUseCase, FakeQueryUseCase, RecordingConversationReplier, RecordingAgentRunStarter, RecordingMarkdownEditor
]:
    query = FakeQueryUseCase()
    replier = RecordingConversationReplier("대화 답변")
    starter = RecordingAgentRunStarter()
    editor = RecordingMarkdownEditor(
        MarkdownEditResult(
            edit=MarkdownEditOperation(
                operation="replace",
                target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1),
                summary="unused",
                replacement_markdown="unused",
            )
        )
    )
    use_case = HandleAgentTurnUseCase(
        router=FixedRouter(route),
        query_use_case=query,  # type: ignore[arg-type]
        markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
        markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
        agent_run_starter=starter,  # type: ignore[arg-type]
        conversation_replier=replier,
    )
    return use_case, query, replier, starter, editor


class TaskModeRequestSchemaTest(unittest.TestCase):
    def test_accepts_task_mode_and_defaults_to_legacy(self) -> None:
        base = {"message": "정리해줘", "provider": "openai", "model": "gpt-5-nano"}

        self.assertEqual(AgentTurnRequestBody.model_validate({**base, "mode": "task"}).to_domain().mode, "task")
        self.assertIsNone(AgentTurnRequestBody.model_validate(base).to_domain().mode)
        with self.assertRaises(ValidationError):
            AgentTurnRequestBody.model_validate({**base, "mode": "wiki"})


class TaskModeRouterTest(unittest.TestCase):
    def test_task_mode_payload_carries_mode_and_legacy_payload_does_not(self) -> None:
        self.assertEqual(route_payload(task_request())["mode"], "task")
        self.assertNotIn("mode", route_payload(AgentTurnRequest(message="질문")))

    def test_task_mode_turns_answer_routes_into_clarify_without_retry(self) -> None:
        for action in TASK_EXCLUDED:
            with self.subTest(action=action):
                client = SequenceJsonClient([route_response(action)])
                router = ChatCompletionsTurnRouter(client, "system")  # type: ignore[arg-type]

                route = router.route(task_request())

                self.assertEqual(route.action, "clarify")
                self.assertEqual(route.retrieval_source, "none")
                self.assertEqual(route.document_operation, "none")
                self.assertIsNone(route.edit_goal)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(json.loads(client.calls[0][1])["mode"], "task")

    def test_legacy_mode_keeps_answer_routes(self) -> None:
        client = SequenceJsonClient([route_response("chat_answer")])
        router = ChatCompletionsTurnRouter(client, "system")  # type: ignore[arg-type]

        route = router.route(AgentTurnRequest(message="회의록 내용이 뭐야?"))

        self.assertEqual(route.action, "chat_answer")

    def test_task_mode_contract_allows_question_clarify_only_in_task_mode(self) -> None:
        clarify = AgentTurnRoute(action="clarify", confidence=0.9, reason="question")

        self.assertEqual(_route_failures(clarify, task_request()), [])
        self.assertTrue(_route_failures(clarify, AgentTurnRequest(message="질문")))
        for action in TASK_EXCLUDED:
            answer = AgentTurnRoute(
                action=action,  # type: ignore[arg-type]
                confidence=0.9,
                reason="answer",
                retrieval_source="workspace" if action == "chat_answer" else "none",
            )
            self.assertTrue(_route_failures(answer, task_request()))

    def test_jev_options_exclude_answer_routes_in_task_mode(self) -> None:
        task_actions = {option["action"] for option in route_options(task_request())}
        legacy_actions = {option["action"] for option in route_options(AgentTurnRequest(message="질문"))}

        self.assertTrue(task_actions.isdisjoint(TASK_EXCLUDED))
        self.assertIn("clarify", task_actions)
        self.assertIn("folder_organize", task_actions)
        self.assertTrue(set(TASK_EXCLUDED).issubset(legacy_actions))
        self.assertNotIn("clarify", legacy_actions)

    def test_router_prompt_documents_task_mode(self) -> None:
        prompt = DEFAULT_AGENT_TURN_ROUTER_PROMPT.read_text(encoding="utf-8")

        self.assertIn("payload.mode", prompt)
        self.assertIn("`chat_answer` and `conversation_reply` are unavailable", prompt)


class TaskModeHandleAgentTurnTest(unittest.TestCase):
    def test_wiki_question_in_task_mode_points_to_chat_tab(self) -> None:
        for action in TASK_EXCLUDED:
            with self.subTest(action=action):
                use_case, query, replier, starter, _ = build_use_case(
                    AgentTurnRoute(
                        action=action,  # type: ignore[arg-type]
                        confidence=0.9,
                        reason="question",
                        retrieval_source="workspace" if action == "chat_answer" else "none",
                    )
                )

                result = use_case.execute(task_request())

                self.assertEqual(result.action, "clarify")
                self.assertEqual(result.message, CLARIFY_TASK_MODE_QUESTION_MESSAGE)
                self.assertIn("채팅 탭", result.message or "")
                self.assertEqual(query.questions, [])
                self.assertEqual(replier.requests, [])
                self.assertEqual(starter.requests, [])

    def test_folder_organize_runs_without_active_document(self) -> None:
        use_case, _, _, starter, editor = build_use_case(
            AgentTurnRoute(
                action="folder_organize",
                confidence=0.95,
                reason="folder request",
                persist=True,
                required_capabilities=("folder-organize",),
            )
        )

        result = use_case.execute(task_request("회의록을 연도별 폴더로 정리해줘"))

        self.assertEqual(result.action, "folder_organize")
        self.assertEqual(result.run_id, "run-1")
        self.assertEqual(getattr(starter.requests[0], "action"), "folder_organize")
        self.assertEqual(editor.requests, [])

    def test_document_edit_without_active_document_asks_to_open_one(self) -> None:
        use_case, _, _, starter, editor = build_use_case(
            AgentTurnRoute(
                action="markdown_edit",
                confidence=0.9,
                reason="edit",
                edit_goal="shorten",
                edit_operation="replace",
                edit_destination="target",
                document_operation="edit",
                required_capabilities=("document-edit",),
            )
        )

        result = use_case.execute(task_request("이 문서를 짧게 줄여줘"))

        self.assertEqual(result.action, "clarify")
        self.assertEqual(result.message, CLARIFY_MARKDOWN_DOCUMENT_MESSAGE)
        self.assertEqual(result.clarify_reason, "markdown_document_required")
        self.assertEqual(editor.requests, [])
        self.assertEqual(starter.requests, [])

    def test_edit_clarify_without_active_document_asks_to_open_one(self) -> None:
        use_case, _, _, _, _ = build_use_case(
            AgentTurnRoute(
                action="clarify",
                confidence=0.5,
                reason="target",
                edit_goal="other",
                edit_operation="insert_after",
                edit_destination="target",
                document_operation="edit",
            )
        )

        result = use_case.execute(task_request("현재 섹션 뒤에 요약을 추가해줘"))

        self.assertEqual(result.message, CLARIFY_MARKDOWN_DOCUMENT_MESSAGE)
        self.assertEqual(result.clarify_reason, "markdown_document_required")

    def test_skill_workflow_edit_without_active_document_has_reason_code(self) -> None:
        use_case, _, _, starter, _ = build_use_case(
            AgentTurnRoute(
                action="workspace_workflow",
                confidence=0.9,
                reason="edit",
                edit_goal="other",
                edit_operation="replace",
                edit_destination="target",
                document_operation="edit",
                persist=True,
                required_capabilities=("document-edit",),
            )
        )

        result = use_case.execute(task_request("이 문서를 고쳐서 저장해줘"))

        self.assertEqual(result.action, "clarify")
        self.assertEqual(result.message, CLARIFY_MARKDOWN_DOCUMENT_MESSAGE)
        self.assertEqual(result.clarify_reason, "markdown_document_required")
        self.assertEqual(starter.requests, [])

    def test_other_clarifications_have_no_reason_code(self) -> None:
        use_case, _, _, _, _ = build_use_case(
            AgentTurnRoute(action="clarify", confidence=0.9, reason="ask", notice_kind="web_disabled")
        )

        result = use_case.execute(task_request())

        self.assertEqual(result.action, "clarify")
        self.assertIsNone(result.clarify_reason)

    def test_clarify_reason_reaches_the_response_payload_next_to_the_unchanged_message(self) -> None:
        use_case, _, _, _, _ = build_use_case(
            AgentTurnRoute(
                action="markdown_edit",
                confidence=0.9,
                reason="edit",
                edit_goal="shorten",
                edit_operation="replace",
                edit_destination="target",
                document_operation="edit",
                required_capabilities=("document-edit",),
            )
        )

        payload = _to_response(use_case.execute(task_request("이 문서를 짧게 줄여줘"))).model_dump(mode="json")

        self.assertEqual(payload["clarify_reason"], "markdown_document_required")
        self.assertEqual(payload["message"], CLARIFY_MARKDOWN_DOCUMENT_MESSAGE)
        self.assertEqual(payload["action"], "clarify")

    def test_legacy_mode_still_answers_questions(self) -> None:
        use_case, query, _, _, _ = build_use_case(
            AgentTurnRoute(
                action="chat_answer",
                confidence=0.9,
                reason="question",
                retrieval_source="workspace",
            )
        )

        result = use_case.execute(
            AgentTurnRequest(message="회의록 내용이 뭐야?", workspace_id="workspace-1", user_id="user-1")
        )

        self.assertEqual(result.action, "chat_answer")
        self.assertEqual(query.questions, ["회의록 내용이 뭐야?"])


CHAT_ROUTE = AgentTurnRoute(action="chat_answer", confidence=0.9, reason="question", retrieval_source="workspace")
REPLY_ROUTE = AgentTurnRoute(action="conversation_reply", confidence=0.9, reason="reply")


class FakeAttachmentReader:
    def __init__(self, response: SkillAuthoringReference | Exception) -> None:
        self.response = response
        self.body_reads = 0

    def read(self, **kwargs: object) -> SkillAuthoringReference:
        if kwargs.get("max_chars") != 1:
            self.body_reads += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def attachment(markdown: str = "다음 회의는 금요일이다.") -> SkillAuthoringReference:
    return SkillAuthoringReference(id="doc-1", name="회의록", markdown=markdown)


def attachment_use_case(
    route: AgentTurnRoute, reader: FakeAttachmentReader
) -> tuple[HandleAgentTurnUseCase, FakeQueryUseCase, RecordingConversationReplier, RecordingMarkdownEditor, list[bool]]:
    query = FakeQueryUseCase()
    replier = RecordingConversationReplier("대화 답변")
    editor = RecordingMarkdownEditor(
        MarkdownEditResult(
            edit=MarkdownEditOperation(
                operation="replace",
                target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1),
                summary="unused",
                replacement_markdown="unused",
            )
        )
    )
    marks: list[bool] = []
    use_case = HandleAgentTurnUseCase(
        router=FixedRouter(route),
        query_use_case=query,  # type: ignore[arg-type]
        markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
        markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
        conversation_replier=replier,
        reference_reader=reader,  # type: ignore[arg-type]
        on_chat_answer=lambda: marks.append(True),
    )
    return use_case, query, replier, editor, marks


def attached_request(message: str = "첨부한 문서만 근거로 답해줘", **kwargs: object) -> AgentTurnRequest:
    """채팅 탭의 첨부 질문. 작업 탭은 첨부가 있어도 답하지 않는다(test_chat_mode)."""
    return AgentTurnRequest(
        message=message, workspace_id="workspace-1", user_id="user-1", mode="chat",
        reference_document_ids=("doc-1",), **kwargs,  # type: ignore[arg-type]
    )


class ChatModeAttachmentRouteTest(unittest.TestCase):
    def test_router_accepts_answer_routes_with_attachments_without_retry(self) -> None:
        for action in TASK_EXCLUDED:
            with self.subTest(action=action):
                client = SequenceJsonClient([route_response(action)])
                router = ChatCompletionsTurnRouter(client, "system")  # type: ignore[arg-type]

                route = router.route(attached_request())

                self.assertEqual(route.action, action)
                self.assertIsNone(route.notice_kind)
                self.assertEqual(len(client.calls), 1)

    def test_contract_allows_answer_routes_only_in_chat_mode(self) -> None:
        self.assertEqual(_route_failures(CHAT_ROUTE, attached_request()), [])
        self.assertEqual(_route_failures(REPLY_ROUTE, attached_request()), [])
        self.assertTrue(_route_failures(CHAT_ROUTE, task_request()))
        self.assertTrue(_route_failures(REPLY_ROUTE, task_request()))
        self.assertTrue(_route_failures(CHAT_ROUTE, task_request(reference_document_ids=("doc-1",))))

    def test_chat_tab_clarify_with_attachments_becomes_grounded_answer(self) -> None:
        for notice_kind in ("chat_tab", None):
            with self.subTest(notice_kind=notice_kind):
                reader = FakeAttachmentReader(attachment())
                use_case, query, _, _, _ = attachment_use_case(
                    AgentTurnRoute(action="clarify", confidence=0.9, reason="question", notice_kind=notice_kind),
                    reader,
                )

                result = use_case.execute(attached_request())

                self.assertEqual(result.action, "chat_answer")
                self.assertEqual(result.route.retrieval_source, "workspace")
                self.assertIsNone(result.route.notice_kind)
                self.assertEqual(len(query.questions), 1)

    def test_other_task_tab_clarifications_with_attachments_are_kept(self) -> None:
        for notice_kind in ("details", "web_disabled", "attachment_target"):
            with self.subTest(notice_kind=notice_kind):
                use_case, query, _, _, _ = attachment_use_case(
                    AgentTurnRoute(action="clarify", confidence=0.9, reason="ask", notice_kind=notice_kind),
                    FakeAttachmentReader(attachment()),
                )

                result = use_case.execute(task_request("첨부한 문서를 고쳐 줘", reference_document_ids=("doc-1",)))

                self.assertEqual(result.action, "clarify")
                self.assertEqual(query.questions, [])
                self.assertNotIn("채팅 탭", result.message or "")


class ChatModeAttachmentAnswerTest(unittest.TestCase):
    def test_question_with_attachment_answers_from_it_and_records_the_branch(self) -> None:
        use_case, query, _, editor, marks = attachment_use_case(CHAT_ROUTE, FakeAttachmentReader(attachment()))

        result = use_case.execute(attached_request())
        payload = _to_response(result).model_dump(mode="json")

        self.assertEqual(result.action, "chat_answer")
        pinned = query.kwargs[0]["pinned_evidence"]
        self.assertEqual(pinned[0].source_document_id, "doc-1")  # type: ignore[index]
        self.assertEqual(marks, [True])
        self.assertEqual(editor.requests, [])
        self.assertEqual(payload["action"], "chat_answer")
        self.assertEqual(payload["chat"]["answer"], "질문 답변입니다.")
        self.assertIsNone(payload["clarify_reason"])

    def test_conversation_reply_with_attachment_reads_it(self) -> None:
        use_case, query, replier, _, marks = attachment_use_case(REPLY_ROUTE, FakeAttachmentReader(attachment()))

        result = use_case.execute(attached_request("첨부 내용을 한 줄로 다듬어줘"))

        self.assertEqual((result.action, result.message), ("conversation_reply", "대화 답변"))
        references = replier.requests[0].conversation_context.reference_context["reference_documents"]  # type: ignore[union-attr]
        self.assertEqual(references[0]["document_id"], "doc-1")  # type: ignore[index]
        self.assertEqual(query.questions, [])
        self.assertEqual(marks, [])

    def test_unreadable_attachment_explains_the_failure_without_answering(self) -> None:
        cases = (
            (SkillRequestRejectedError("invalid_reference", "not accessible"), REFERENCE_NOT_ACCESSIBLE_MESSAGE),
            (RuntimeError("down"), REFERENCE_READ_FAILED_MESSAGE),
            (ReferenceDocumentTooLargeError(), REFERENCE_ALL_TOO_LONG_MESSAGE),
        )
        for route in (CHAT_ROUTE, REPLY_ROUTE):
            for error, message in cases:
                with self.subTest(action=route.action, error=type(error).__name__):
                    reader = FakeAttachmentReader(error)
                    use_case, query, replier, _, marks = attachment_use_case(route, reader)

                    result = use_case.execute(attached_request())

                    self.assertEqual((result.action, result.message), ("clarify", message))
                    self.assertEqual(reader.body_reads, 1)
                    self.assertEqual(query.questions, [])
                    self.assertEqual(replier.requests, [])
                    self.assertEqual(marks, [])

    def test_answer_with_open_document_does_not_touch_the_document(self) -> None:
        use_case, query, _, editor, _ = attachment_use_case(CHAT_ROUTE, FakeAttachmentReader(attachment()))

        result = use_case.execute(
            attached_request(
                "첨부한 문서에서 다음 회의가 언제야?",
                active_markdown_context=ActiveMarkdownContext(markdown="# 열린 문서"),
                document_id="open-doc",
                base_version=3,
            )
        )

        self.assertEqual(result.action, "chat_answer")
        self.assertIsNone(result.edit)
        self.assertIsNone(result.run_id)
        self.assertEqual(editor.requests, [])
        self.assertEqual(  # 첨부만 지목한 질문은 열린 문서를 근거로 섞지 않는다.
            {snippet.source_document_id for snippet in query.kwargs[0]["pinned_evidence"]}, {"doc-1"}  # type: ignore[union-attr]
        )

    def test_open_document_in_task_tab_points_to_chat_tab(self) -> None:
        use_case, query, _, _, _ = attachment_use_case(CHAT_ROUTE, FakeAttachmentReader(attachment()))

        result = use_case.execute(
            task_request(
                active_markdown_context=ActiveMarkdownContext(markdown="# 열린 문서"),
                document_id="open-doc",
                base_version=3,
            )
        )

        self.assertEqual(result.message, CLARIFY_TASK_MODE_QUESTION_MESSAGE)
        self.assertEqual(result.route.notice_kind, "chat_tab")
        self.assertEqual(query.questions, [])


class TaskModeAttachmentPromptTest(unittest.TestCase):
    def test_prompt_routes_attachment_questions_and_summaries_to_answers(self) -> None:
        prompt = DEFAULT_AGENT_TURN_ROUTER_PROMPT.read_text(encoding="utf-8")

        self.assertIn("with or without attachments or open Markdown", prompt)
        self.assertNotIn("a request to summarize them into a result is `markdown_create`", prompt)


if __name__ == "__main__":
    unittest.main()
