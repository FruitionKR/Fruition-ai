import json
import unittest
from datetime import datetime, timezone

from app.modules.agent.domain.entities import AgentConversationContext, AgentTurnRequest
from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
from app.modules.agent.infrastructure.chat_completions_conversation_replier import (
    DEFAULT_CONVERSATION_REPLY_PROMPT,
    ChatCompletionsConversationReplier,
    _current_date,
)
from app.modules.query.domain.entities import ConversationMessage
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError


class FakeChatClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        trusted_identifiers: tuple[str, ...] = (),
    ) -> dict[str, object]:
        self.calls.append((system_prompt, user_prompt, trusted_identifiers))
        return {
            "message": "  2026-08-17-덥고 습함-🥵  ",
        }


class SequenceJsonClient:
    def __init__(self, responses: list[dict[str, object] | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        trusted_identifiers: tuple[str, ...] = (),
    ) -> dict[str, object]:
        self.calls.append((system_prompt, user_prompt, trusted_identifiers))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ChatCompletionsConversationReplierTest(unittest.TestCase):
    def test_uses_korean_product_date_at_utc_day_boundary(self) -> None:
        utc_time = datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)

        self.assertEqual(_current_date(utc_time), "2026-08-17")

    def test_prompt_requires_recent_values_instead_of_placeholders(self) -> None:
        prompt = DEFAULT_CONVERSATION_REPLY_PROMPT.read_text(encoding="utf-8")

        self.assertIn("Reuse the most recent user-provided value", prompt)
        self.assertIn("Never replace a value", prompt)
        self.assertIn("Do not reclassify", prompt)

    def test_replies_from_current_and_recent_conversation_without_retrieval(self) -> None:
        client = FakeChatClient()
        replier = ChatCompletionsConversationReplier(client, "system")  # type: ignore[arg-type]

        reply = replier.reply(
            AgentTurnRequest(
                message="오늘날짜-날씨-이모지 한 개 형식으로 만들어줘",
                conversation_context=AgentConversationContext(
                    recent_messages=(
                        ConversationMessage(role="user", content="여름이어서 덥고 습했다"),
                        ConversationMessage(
                            role="assistant",
                            content="제목을 만들어 드릴게요.",
                            action="conversation_reply",
                        ),
                    ),
                ),
            )
        )

        self.assertEqual(reply, "2026-08-17-덥고 습함-🥵")
        system_prompt, user_prompt, trusted_identifiers = client.calls[0]
        payload = json.loads(user_prompt)
        self.assertIn("Current date:", system_prompt)
        self.assertEqual(payload["recent_messages"][1]["action"], "conversation_reply")
        self.assertEqual(len(trusted_identifiers), 1)

    def test_rejects_empty_reply(self) -> None:
        client = FakeChatClient()
        client.complete_json = lambda *args, **kwargs: {  # type: ignore[method-assign]
            "message": None,
        }
        replier = ChatCompletionsConversationReplier(client, "system")  # type: ignore[arg-type]

        with self.assertRaises(AgentTurnRouteContractError) as raised:
            replier.reply(AgentTurnRequest(message="제목을 만들어줘."))

        self.assertEqual(raised.exception.failures, ["message must be a non-empty string"])

    def test_retries_json_parse_failure_once(self) -> None:
        client = SequenceJsonClient(
            [
                JsonParseError("secret malformed reply"),
                {
                    "message": "다시 작성한 답변",
                },
            ]
        )
        replier = ChatCompletionsConversationReplier(client, "system")  # type: ignore[arg-type]

        reply = replier.reply(AgentTurnRequest(message="다시 설명해줘."))

        self.assertEqual(reply, "다시 작성한 답변")
        retry_payload = json.loads(client.calls[1][1])
        self.assertEqual(
            retry_payload["contract_failures"],
            ["conversation reply output must be a JSON object"],
        )
        self.assertNotIn("secret malformed reply", client.calls[1][1])

    def test_converts_second_json_parse_failure_to_contract_error(self) -> None:
        client = SequenceJsonClient(
            [
                JsonParseError("first malformed reply"),
                JsonParseError("second malformed reply"),
            ]
        )
        replier = ChatCompletionsConversationReplier(client, "system")  # type: ignore[arg-type]

        with self.assertRaises(AgentTurnRouteContractError) as raised:
            replier.reply(AgentTurnRequest(message="다시 설명해줘."))

        self.assertEqual(
            raised.exception.failures,
            ["conversation reply output must be a JSON object"],
        )


if __name__ == "__main__":
    unittest.main()
