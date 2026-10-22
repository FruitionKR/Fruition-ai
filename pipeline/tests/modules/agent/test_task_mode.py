"""작업 탭(mode=task) 턴의 라우팅·실행 규칙."""

import json
import unittest

from pydantic import ValidationError

from app.modules.agent.application.handle_agent_turn import (
    CLARIFY_MARKDOWN_DOCUMENT_MESSAGE,
    CLARIFY_TASK_MODE_QUESTION_MESSAGE,
    HandleAgentTurnUseCase,
)
from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    DEFAULT_AGENT_TURN_ROUTER_PROMPT,
    ChatCompletionsTurnRouter,
    _route_failures,
    route_payload,
)
from app.modules.agent.infrastructure.jev_turn_router import route_options
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
            AgentTurnRequestBody.model_validate({**base, "mode": "chat"})


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


if __name__ == "__main__":
    unittest.main()
