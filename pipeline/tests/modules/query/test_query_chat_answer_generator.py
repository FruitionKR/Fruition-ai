import unittest
from unittest.mock import patch

from app.modules.query.domain.entities import ConversationMessage, GraphContext, QueryContext
from app.modules.query.infrastructure.query_chat_answer_generator import (
    QUERY_ANSWER_SYSTEM_PROMPT,
    QueryChatAnswerGenerator,
    QueryConversationSummarizer,
    build_query_conversation_summarizer,
    _config_from_env,
)
from app.modules.wiki_generation.infrastructure.chat_completions_llm import ChatClientConfig


class FakeChatClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete_text(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return "  한국어 답변입니다.  "


class QueryChatAnswerGeneratorTest(unittest.TestCase):
    def test_generates_answer_from_query_context(self) -> None:
        client = FakeChatClient()
        generator = QueryChatAnswerGenerator(client)
        context = QueryContext(
            question="질문",
            graph_context=GraphContext(),
            traversal_paths=[],
            related_pages=[],
            evidence_snippets=[],
            answer_context="# User Question\n질문",
        )

        answer = generator.generate_answer(context)

        self.assertEqual(answer.content, "한국어 답변입니다.")
        self.assertEqual(client.calls[0][1], "# User Question\n질문")
        self.assertIn("Answer in Korean.", client.calls[0][0])
        self.assertIn("citation markers like [1]", client.calls[0][0])
        self.assertEqual(client.calls[0][0], QUERY_ANSWER_SYSTEM_PROMPT)

    def test_request_snapshot_overrides_legacy_environment_model(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GEMINI_API_KEY": "secret",
                "LLM_API_KEY": "legacy-key",
                "LLM_MODEL": "environment-model",
            },
            clear=True,
        ):
            config = _config_from_env(provider="gemini", model="gemini-3.1-flash-lite")

        self.assertEqual(config.model, "gemini-3.1-flash-lite")
        self.assertEqual(config.api_key, "secret")
        self.assertEqual(config.provider, "gemini")

    def test_adds_trusted_language_and_length_preferences_to_system_prompt(self) -> None:
        client = FakeChatClient()
        generator = QueryChatAnswerGenerator(client)
        context = QueryContext(
            question="Explain it",
            graph_context=GraphContext(),
            traversal_paths=[],
            related_pages=[],
            evidence_snippets=[],
            answer_context="# User Question\nExplain it",
            output_language="en",
            response_length="detailed",
        )

        generator.generate_answer(context)

        system_prompt = client.calls[0][0]
        self.assertIn("# Response Preferences", system_prompt)
        self.assertIn("Write the response in English.", system_prompt)
        self.assertIn("Give a detailed explanation", system_prompt)
        self.assertNotIn("output_language", client.calls[0][1])

    def test_updates_conversation_summary_from_previous_summary_and_messages(self) -> None:
        client = FakeChatClient()
        summarizer = QueryConversationSummarizer(client)

        summary = summarizer.summarize(
            "기존에는 Persistent Wiki를 논의했다.",
            (
                ConversationMessage(role="user", content="RAG와 비교해줘"),
                ConversationMessage(role="assistant", content="축적 방식이 다릅니다."),
            ),
        )

        self.assertEqual(summary, "한국어 답변입니다.")
        self.assertIn("기존에는 Persistent Wiki", client.calls[0][1])
        self.assertIn("사용자: RAG와 비교해줘", client.calls[0][1])

    def test_conversation_summarizer_uses_request_snapshot(self) -> None:
        config = ChatClientConfig(
            api_key="test-key",
            model="gpt-5-nano",
            provider="openai",
        )

        with patch(
            "app.modules.query.infrastructure.query_chat_answer_generator._config_from_env",
            return_value=config,
        ) as config_from_env:
            build_query_conversation_summarizer(provider="openai", model="gpt-5-nano")

        self.assertEqual(config.provider, "openai")
        config_from_env.assert_called_once_with(provider="openai", model="gpt-5-nano")


if __name__ == "__main__":
    unittest.main()
