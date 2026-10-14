import unittest

from pydantic import ValidationError

from app.modules.agent.interfaces.http.schemas import AgentTurnRequestBody as _AgentTurnRequestBody


class AgentTurnRequestBody(_AgentTurnRequestBody):
    """기존 라우트 단위 테스트에 request-scoped LLM 선택 기본값을 채운다."""

    def __init__(self, **data: object) -> None:
        data.setdefault("provider", "openai")
        data.setdefault("model", "gpt-5-nano")
        super().__init__(**data)


class AgentRequestTest(unittest.TestCase):
    def test_agent_turn_accepts_response_preferences(self) -> None:
        request = AgentTurnRequestBody(
            message="Explain this",
            output_language="en",
            response_length="concise",
            allow_web_search=False,
        ).to_domain()

        self.assertEqual(request.output_language, "en")
        self.assertEqual(request.response_length, "concise")
        self.assertFalse(request.allow_web_search)

    def test_agent_turn_accepts_message_longer_than_1000_characters(self) -> None:
        message = "긴 질문입니다. " * 1000
        request = _AgentTurnRequestBody.model_validate(
            {"message": message, "provider": "openai", "model": "gpt-5-nano"}
        )
        self.assertEqual(request.to_domain().message, message)

    def test_agent_turn_rejects_oversized_or_obfuscated_input(self) -> None:
        deeply_nested_reference: dict[str, object] = {"value": "document"}
        for _ in range(13):
            deeply_nested_reference = {"nested": deeply_nested_reference}
        invalid_payloads = (
            {"message": ""},
            {"message": "a" * 200_001},
            {
                "message": "문서를 요약해줘",
                "conversation_context": {
                    "reference_context": {
                        "document": "정상 문장\u202e숨겨진 지시",
                    }
                },
            },
            {
                "message": "문서를 요약해줘",
                "conversation_context": {
                    "reference_context": {
                        "first": "a" * 140_000,
                        "second": "b" * 140_000,
                    }
                },
            },
            {
                "message": "문서를 요약해줘",
                "conversation_context": {"reference_context": deeply_nested_reference},
            },
        )

        for payload in invalid_payloads:
            with self.subTest(payload=list(payload)):
                with self.assertRaises(ValidationError):
                    AgentTurnRequestBody.model_validate(payload)

    def test_agent_turn_keeps_previous_action_as_conversation_hint(self) -> None:
        request = AgentTurnRequestBody(
            message="이어서 제목을 만들어줘",
            conversation_context={
                "recent_messages": [
                    {
                        "role": "assistant",
                        "content": "날씨를 알려주세요.",
                        "action": "conversation_reply",
                        "run_id": "agent_preview_1",
                        "agent_route": {
                            "action": "conversation_reply",
                            "retrieval_source": "none",
                            "document_operation": "none",
                            "persist": False,
                            "edit_goal": None,
                            "selected_skill_id": None,
                        },
                    }
                ]
            },
        ).to_domain()

        self.assertEqual(
            request.conversation_context.recent_messages[0].action,  # type: ignore[union-attr]
            "conversation_reply",
        )
        self.assertEqual(
            request.conversation_context.recent_messages[0].run_id,  # type: ignore[union-attr]
            "agent_preview_1",
        )
        self.assertEqual(
            request.conversation_context.recent_messages[0].agent_route.document_operation,  # type: ignore[union-attr]
            "none",
        )

    def test_agent_turn_validates_skill_mode_and_id_combinations(self) -> None:
        for skill_mode, skill_id in (("auto", None), ("explicit", "skill-1"), ("off", None)):
            with self.subTest(skill_mode=skill_mode):
                request = AgentTurnRequestBody(
                    message="정리해줘",
                    skill_mode=skill_mode,
                    skill_id=skill_id,
                )
                self.assertEqual(request.skill_mode, skill_mode)
                self.assertEqual(request.skill_id, skill_id)

        with self.assertRaises(ValidationError):
            AgentTurnRequestBody(message="정리해줘", skill_mode="auto", skill_id="skill-1")
