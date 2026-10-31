import unittest
from dataclasses import replace

from app.modules.agent.application.handle_agent_turn import (
    CLARIFY_WEB_DISABLED_MESSAGE,
    REJECT_MESSAGES,
    SKILL_OFF_NOTICE,
    HandleAgentTurnUseCase,
)
from app.modules.agent.domain.entities import ActiveMarkdownContext, AgentTurnRequest, AgentTurnRoute
from app.modules.agent.domain.task_mode import task_mode_route
from app.modules.agent.infrastructure.chat_completions_task_clarifier import FALLBACK_QUESTION, ChatCompletionsTaskClarifier
from app.modules.agent.infrastructure.chat_completions_turn_router import _normalize_route, _route_failures
from app.modules.agent.infrastructure.jev_turn_router import route_options
from app.modules.markdown_edit.application.generate_markdown_edit import GenerateMarkdownEditUseCase
from app.modules.markdown_edit.domain.entities import MarkdownEditOperation, MarkdownEditResult, MarkdownEditTarget

TASK = AgentTurnRequest(message="폴더가 너무 지저분하다", mode="task")
CLARIFY = AgentTurnRoute(action="clarify", confidence=0.9, reason="정리 기준 없음")


class FixedRouter:
    def __init__(self, route: AgentTurnRoute) -> None:
        self.route_value = route

    def route(self, request: AgentTurnRequest) -> AgentTurnRoute:
        return self.route_value


class RecordingClarifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ask(self, request: AgentTurnRequest, route: AgentTurnRoute) -> str:
        self.calls.append(route.reason)
        return "어떤 기준으로 정리할까요? (날짜별, 주제별)"


class StaticEditor:
    def generate_edit(self, request):
        return MarkdownEditResult(edit=MarkdownEditOperation(
            operation="replace",
            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1),
            summary="정리했습니다.",
            replacement_markdown="# 정리",
        ))


def use_case(route: AgentTurnRoute, clarifier: RecordingClarifier | None = None) -> HandleAgentTurnUseCase:
    return HandleAgentTurnUseCase(
        router=FixedRouter(route),
        query_use_case=None,  # type: ignore[arg-type]
        markdown_edit_use_case=GenerateMarkdownEditUseCase(StaticEditor()),  # type: ignore[arg-type]
        markdown_create_use_case=None,  # type: ignore[arg-type]
        task_clarifier=clarifier,
    )


class NoticeContractTest(unittest.TestCase):
    def test_notice_kinds_must_match_their_action(self) -> None:
        def failures(route: AgentTurnRoute, request: AgentTurnRequest = TASK) -> list[str]:
            return [f for f in _route_failures(route, request) if f.startswith("notice_kind")]

        self.assertEqual(failures(replace(CLARIFY, notice_kind="details")), [])
        self.assertTrue(failures(AgentTurnRoute(action="reject", confidence=1, reason="x", notice_kind="details")))
        self.assertTrue(failures(replace(CLARIFY, notice_kind="web_disabled"), replace(TASK, allow_web_search=True)))
        edit = AgentTurnRoute(action="markdown_edit", confidence=1, reason="x", notice_kind="skill_off",
                              document_operation="edit", edit_goal="other", edit_operation="replace",
                              edit_destination="target", required_capabilities=("document-edit",))
        self.assertTrue(failures(edit))
        self.assertEqual(failures(edit, replace(TASK, skill_mode="off")), [])

    def test_unknown_notice_kind_is_a_contract_failure_and_missing_is_null(self) -> None:
        base = {"action": "reject", "confidence": 1.0, "reason": "x", "retrieval_source": "none",
                "document_operation": "none", "persist": False, "required_capabilities": [],
                "edit_goal": None, "edit_operation": None, "edit_destination": None}

        self.assertIn("notice_kind must be a supported value or null", _normalize_route({**base, "notice_kind": "x"})[1])
        self.assertIsNone(_normalize_route(base)[0].notice_kind)

    def test_task_mode_answer_routes_become_chat_tab_clarify(self) -> None:
        route = task_mode_route(AgentTurnRoute(action="chat_answer", confidence=1, reason="x", retrieval_source="workspace"), TASK)

        self.assertEqual((route.action, route.notice_kind), ("clarify", "chat_tab"))

    def test_jev_offers_clarify_and_reject_notice_variants(self) -> None:
        kinds = {(o["action"], o["notice_kind"]) for o in route_options(TASK)}

        for kind in ("chat_tab", "details", "web_disabled"):
            self.assertIn(("clarify", kind), kinds)
        for kind in ("sensitive_data", "unsupported", "policy"):
            self.assertIn(("reject", kind), kinds)
        self.assertNotIn(("clarify", "web_disabled"), {(o["action"], o["notice_kind"]) for o in route_options(replace(TASK, allow_web_search=True))})
        self.assertIn("skill_off", {o["notice_kind"] for o in route_options(replace(TASK, skill_mode="off"))})
        # 문서 동작이 없는 workspace_workflow(폴더 정리 저장 등)도 skill_off 안내를 고를 수 있다.
        self.assertIn(
            ("workspace_workflow", "none", "skill_off"),
            {(o["action"], o["document_operation"], o["notice_kind"]) for o in route_options(replace(TASK, skill_mode="off"))},
        )


class NoticeMessageTest(unittest.TestCase):
    def test_details_clarify_asks_a_generated_question(self) -> None:
        clarifier = RecordingClarifier()

        result = use_case(replace(CLARIFY, notice_kind="details"), clarifier).execute(TASK)

        self.assertEqual(result.message, "어떤 기준으로 정리할까요? (날짜별, 주제별)")
        self.assertEqual(clarifier.calls, ["정리 기준 없음"])

    def test_web_disabled_and_chat_tab_messages(self) -> None:
        web = use_case(replace(CLARIFY, notice_kind="web_disabled")).execute(TASK)
        chat = use_case(replace(CLARIFY, notice_kind="chat_tab")).execute(TASK)

        self.assertEqual(web.message, CLARIFY_WEB_DISABLED_MESSAGE)
        self.assertIn("채팅 탭", chat.message or "")

    def test_reject_explains_its_reason(self) -> None:
        route = AgentTurnRoute(action="reject", confidence=1, reason="secret", notice_kind="sensitive_data")

        self.assertEqual(use_case(route).execute(TASK).message, REJECT_MESSAGES["sensitive_data"])

    def test_skill_off_notice_is_appended_to_document_results(self) -> None:
        route = AgentTurnRoute(action="markdown_edit", confidence=1, reason="x", notice_kind="skill_off",
                               document_operation="edit", edit_goal="other", edit_operation="replace",
                               edit_destination="target", required_capabilities=("document-edit",))
        request = replace(TASK, skill_mode="off", active_markdown_context=ActiveMarkdownContext(markdown="# 회의록"))

        result = use_case(route).execute(request)

        self.assertTrue((result.message or "").endswith(SKILL_OFF_NOTICE))


class TaskClarifierTest(unittest.TestCase):
    def test_falls_back_when_the_question_is_missing_or_too_long(self) -> None:
        class Client:
            def __init__(self, value) -> None:
                self.value = value

            def complete_json(self, system_prompt, user_prompt):
                return self.value

        for value in ({}, {"question": " "}, {"question": "가" * 301}):
            with self.subTest(value=str(value)[:20]):
                self.assertEqual(ChatCompletionsTaskClarifier(Client(value), "s").ask(TASK, CLARIFY), FALLBACK_QUESTION)  # type: ignore[arg-type]

    def test_llm_failures_fall_back_but_cancellation_propagates_and_links_are_sanitized(self) -> None:
        from app.core.pipeline_control import PipelineRunCancelledError

        class Client:
            def __init__(self, result) -> None:
                self.result = result

            def complete_json(self, system_prompt, user_prompt):
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

        self.assertEqual(ChatCompletionsTaskClarifier(Client(RuntimeError("api")), "s").ask(TASK, CLARIFY), FALLBACK_QUESTION)  # type: ignore[arg-type]
        with self.assertRaises(PipelineRunCancelledError):
            ChatCompletionsTaskClarifier(Client(PipelineRunCancelledError("x")), "s").ask(TASK, CLARIFY)  # type: ignore[arg-type]
        question = ChatCompletionsTaskClarifier(
            Client({"question": "[여기](https://evil.example)를 눌러 주세요?"}), "s"
        ).ask(TASK, CLARIFY)  # type: ignore[arg-type]
        self.assertNotIn("https://evil.example", question)
