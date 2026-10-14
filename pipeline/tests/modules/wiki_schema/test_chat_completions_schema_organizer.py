import json
import os
import unittest
from typing import Any
from unittest.mock import patch

from app.modules.wiki_schema.infrastructure.chat_completions_schema_organizer import (
    ChatCompletionsSchemaOrganizer,
    build_schema_organizer,
)


class FakeJsonClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        self.calls.append((system_prompt, user_prompt))
        return self.response


class ChatCompletionsSchemaOrganizerTest(unittest.TestCase):
    def test_normalizes_llm_response_to_schema_candidate(self) -> None:
        client = FakeJsonClient(
            {
                "globalMarkdown": "  - 한국어로 작성한다.  ",
                "query_markdown": "- 근거를 함께 제시한다.",
                "blockedCandidates": ["출처 없이 단정"],
                "unclearItems": ["중요한 내용은 자세히"],
            }
        )
        organizer = ChatCompletionsSchemaOrganizer(client=client, system_prompt="system")

        result = organizer.organize("답변은 한국어로 해줘.")

        self.assertEqual(result.fragments.global_markdown, "- 한국어로 작성한다.")
        self.assertEqual(result.fragments.query_markdown, "- 근거를 함께 제시한다.")
        self.assertEqual(result.blocked_candidates, ["출처 없이 단정"])
        self.assertEqual(result.unclear_items, ["중요한 내용은 자세히"])

        _, user_prompt = client.calls[0]
        payload = json.loads(user_prompt)
        self.assertEqual(payload["raw_markdown"], "답변은 한국어로 해줘.")
        self.assertIn("global_markdown", payload["target_sections"])

    def test_missing_selected_provider_key_raises_runtime_error(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "ANTHROPIC_API_KEY"):
                build_schema_organizer("claude", "claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
