import unittest
from unittest.mock import patch

from app.core.jev_client import JevUnavailable
from app.modules.agent.domain.entities import (
    AgentTurnRequest,
    AgentTurnRoute,
    SkillCandidate,
)
from app.modules.agent.infrastructure.jev_turn_router import (
    JevTurnRouter,
    route_options,
    with_jev_routing,
)


class RecordingRouter:
    def __init__(self) -> None:
        self.requests: list[AgentTurnRequest] = []

    def route(self, request: AgentTurnRequest) -> AgentTurnRoute:
        self.requests.append(request)
        return AgentTurnRoute(action="conversation_reply", confidence=0.5, reason="기존 라우터")


class ChoosingJev:
    """criteria 값이 조건에 맞는 첫 선택지를 고른다."""

    def __init__(self, *predicates) -> None:
        self._predicates = list(predicates)
        self.calls: list[dict] = []

    def choose(self, state: str, questions: dict) -> dict:
        self.calls.append(questions)
        predicate = self._predicates.pop(0)
        criteria = questions["route"]["criteria"]
        choice = next(key for key, value in criteria.items() if predicate(value))
        return {"route": {"choice": choice, "confidence": 0.7}}


class FailingJev:
    def choose(self, state: str, questions: dict) -> dict:
        raise JevUnavailable("Jev HTTP 402")


def _skill(skill_id: str, capabilities: tuple[str, ...]) -> SkillCandidate:
    return SkillCandidate(
        id=skill_id,
        version_id=f"{skill_id}-v1",
        name=skill_id,
        description="",
        capabilities=capabilities,
    )


class JevTurnRouterTest(unittest.TestCase):
    def test_selects_route_from_valid_options(self) -> None:
        jev = ChoosingJev(lambda value: value["action"] == "chat_answer" and value["retrieval_source"] == "workspace")
        fallback = RecordingRouter()

        route = JevTurnRouter(jev, fallback, "system").route(AgentTurnRequest(message="정책 알려줘"))

        self.assertEqual(route.action, "chat_answer")
        self.assertEqual(route.retrieval_source, "workspace")
        self.assertEqual(route.confidence, 0.7)
        self.assertEqual(fallback.requests, [])

    def test_edit_route_chooses_edit_details_in_second_stage(self) -> None:
        jev = ChoosingJev(
            lambda value: value["action"] == "markdown_edit",
            lambda value: value["edit_goal"] == "shorten" and value["edit_destination"] == "target",
        )

        route = JevTurnRouter(jev, RecordingRouter(), "system").route(AgentTurnRequest(message="줄여줘"))

        self.assertEqual(route.action, "markdown_edit")
        self.assertEqual(route.edit_goal, "shorten")
        self.assertEqual(route.edit_operation, "replace")
        self.assertNotIn("edit_goal", next(iter(jev.calls[0]["route"]["criteria"].values())))
        self.assertIn("edit_goal", next(iter(jev.calls[1]["route"]["criteria"].values())))

    def test_unavailable_jev_uses_existing_router(self) -> None:
        fallback = RecordingRouter()

        route = JevTurnRouter(FailingJev(), fallback, "system").route(AgentTurnRequest(message="안녕"))

        self.assertEqual(route.reason, "기존 라우터")
        self.assertEqual(len(fallback.requests), 1)

    def test_overlapping_skills_use_existing_router_for_clarification(self) -> None:
        jev = ChoosingJev()
        fallback = RecordingRouter()
        request = AgentTurnRequest(
            message="보고서 만들어줘",
            available_skills=(_skill("a", ("document-create",)), _skill("b", ("template",))),
        )

        JevTurnRouter(jev, fallback, "system").route(request)

        self.assertEqual(jev.calls, [])
        self.assertEqual(fallback.requests, [request])

    def test_options_exclude_clarify_and_pass_route_contract(self) -> None:
        request = AgentTurnRequest(message="x", available_skills=(_skill("a", ("document-edit",)),))

        options = route_options(request)

        self.assertTrue(options)
        self.assertNotIn("clarify", {option["action"] for option in options})
        self.assertIn("a", {option["selected_skill_id"] for option in options})


class WithJevRoutingTest(unittest.TestCase):
    def test_keeps_existing_router_when_disabled(self) -> None:
        router = RecordingRouter()
        with patch.dict("os.environ", {"JEV_ROUTING_ENABLED": "false", "TYPESAFE_API_KEY": "key"}):
            self.assertIs(with_jev_routing(router), router)

    def test_wraps_router_when_enabled_with_key(self) -> None:
        with patch.dict("os.environ", {"JEV_ROUTING_ENABLED": "true", "TYPESAFE_API_KEY": "key"}):
            self.assertIsInstance(with_jev_routing(RecordingRouter()), JevTurnRouter)


if __name__ == "__main__":
    unittest.main()
