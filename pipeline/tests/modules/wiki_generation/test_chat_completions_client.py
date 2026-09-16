import unittest
from contextlib import nullcontext
from unittest.mock import patch

from langchain_core.messages import AIMessage

from app.modules.wiki_generation.infrastructure import chat_completions_llm
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)


class _Model:
    def __init__(
        self,
        response: AIMessage | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or AIMessage(content='{"result":"ok"}')
        self.error = error
        self.bind_kwargs: dict[str, object] = {}
        self.messages: list[object] = []

    def bind(self, **kwargs: object) -> "_Model":
        self.bind_kwargs = kwargs
        return self

    def invoke(self, messages: list[object]) -> AIMessage:
        self.messages = messages
        if self.error is not None:
            raise self.error
        return self.response


class _HttpError(RuntimeError):
    status_code = 429


class ChatCompletionsJsonClientTest(unittest.TestCase):
    def test_temperature_is_omitted_unless_caller_sets_it(self) -> None:
        config = ChatClientConfig(
            api_key="provider-key",
            model="claude-sonnet-5",
            provider="claude",
        )

        self.assertIsNone(config.temperature)

    def test_provider_models_receive_generation_and_retry_config(self) -> None:
        cases = (
            (
                "openai",
                "gpt-5-nano",
                "ChatOpenAI",
                {"reasoning_effort": "medium"},
            ),
            (
                "gemini",
                "gemini-3.1-flash-lite",
                "ChatGoogleGenerativeAI",
                {"thinking_level": "low"},
            ),
            ("claude", "claude-sonnet-5", "ChatAnthropic", {}),
        )
        for provider, model_name, class_name, provider_options in cases:
            with self.subTest(provider=provider):
                model = _Model()
                with patch.object(
                    chat_completions_llm,
                    class_name,
                    return_value=model,
                    create=True,
                ) as constructor:
                    ChatCompletionsJsonClient(
                        ChatClientConfig(
                            api_key="provider-key",
                            model=model_name,
                            temperature=0.0,
                            timeout_seconds=17,
                            max_tokens=321,
                            max_retries=3,
                            provider=provider,
                        )
                    )

                constructor.assert_called_once_with(
                    model=model_name,
                    api_key="provider-key",
                    temperature=0.0,
                    timeout=17,
                    max_tokens=321,
                    max_retries=3,
                    **provider_options,
                )

    def test_json_mode_uses_each_provider_contract(self) -> None:
        cases = (
            ("openai", "gpt-5-nano", "ChatOpenAI"),
            ("gemini", "gemini-3.1-flash-lite", "ChatGoogleGenerativeAI"),
            ("claude", "claude-sonnet-5", "ChatAnthropic"),
        )
        for provider, model_name, class_name in cases:
            with self.subTest(provider=provider):
                model = _Model()
                with patch.object(
                    chat_completions_llm,
                    class_name,
                    return_value=model,
                    create=True,
                ) as constructor:
                    client = ChatCompletionsJsonClient(
                        ChatClientConfig(
                            api_key="provider-key",
                            model=model_name,
                            json_mode=True,
                            provider=provider,
                        )
                    )
                    self.assertEqual(
                        client.complete_json("system prompt", "user prompt"),
                        {"result": "ok"},
                    )

                system_content = model.messages[0].content  # type: ignore[attr-defined]
                if provider == "openai":
                    self.assertEqual(
                        model.bind_kwargs,
                        {"response_format": {"type": "json_object"}},
                    )
                elif provider == "gemini":
                    self.assertEqual(
                        constructor.call_args.kwargs["response_mime_type"],
                        "application/json",
                    )
                else:
                    self.assertIn("Return only one valid JSON object", system_content)

    def test_masks_request_and_response_numeric_personal_data(self) -> None:
        model = _Model(AIMessage(content="연락처는 010-1234-5678입니다."))
        with patch.object(
            chat_completions_llm,
            "ChatOpenAI",
            return_value=model,
            create=True,
        ):
            client = ChatCompletionsJsonClient(
                ChatClientConfig(
                    api_key="provider-key",
                    model="gpt-5-nano",
                    provider="openai",
                )
            )
            content = client.complete_text(
                "system prompt",
                "연락처는 010-9876-5432입니다.",
            )

        self.assertIn("highest-priority", model.messages[0].content)  # type: ignore[attr-defined]
        self.assertNotIn("010-9876-5432", model.messages[1].content)  # type: ignore[attr-defined]
        self.assertIn("[NUMERIC_PERSONAL_DATA]", model.messages[1].content)  # type: ignore[attr-defined]
        self.assertEqual(content, "연락처는 [NUMERIC_PERSONAL_DATA]입니다.")

    def test_preserves_trusted_identifier_in_request_and_response(self) -> None:
        document_id = "doc_5d1d66f111584257813657ddae1a4eea"
        model = _Model(AIMessage(content=f'{{"result":"{document_id}"}}'))
        with patch.object(
            chat_completions_llm,
            "ChatOpenAI",
            return_value=model,
            create=True,
        ):
            client = ChatCompletionsJsonClient(
                ChatClientConfig(
                    api_key="provider-key",
                    model="gpt-5-nano",
                    json_mode=True,
                    provider="openai",
                )
            )
            result = client.complete_json(
                "system prompt",
                f"target {document_id}, 카드 4111 1111 1111 1111",
                trusted_identifiers=(document_id,),
            )

        user_content = model.messages[1].content  # type: ignore[attr-defined]
        self.assertIn(document_id, user_content)
        self.assertNotIn("4111 1111 1111 1111", user_content)
        self.assertEqual(result, {"result": document_id})

    def test_uses_message_text_for_gemini_content_blocks(self) -> None:
        model = _Model(
            AIMessage(
                content=[
                    {
                        "type": "text",
                        "text": '{"result":"ok"}',
                        "extras": {"signature": "signed"},
                    }
                ]
            )
        )
        with patch.object(
            chat_completions_llm,
            "ChatGoogleGenerativeAI",
            return_value=model,
            create=True,
        ):
            client = ChatCompletionsJsonClient(
                ChatClientConfig(
                    api_key="provider-key",
                    model="gemini-3.1-flash-lite",
                    provider="gemini",
                )
            )

            self.assertEqual(client.complete_text("system", "user"), '{"result":"ok"}')

    def test_normalizes_model_errors_without_leaking_numeric_data(self) -> None:
        model = _Model(error=_HttpError("retry after calling 010-1234-5678"))
        with patch.object(
            chat_completions_llm,
            "ChatOpenAI",
            return_value=model,
            create=True,
        ):
            client = ChatCompletionsJsonClient(
                ChatClientConfig(
                    api_key="provider-key",
                    model="gpt-5-nano",
                    provider="openai",
                )
            )

            with self.assertRaisesRegex(RuntimeError, "LLM API HTTP 429") as raised:
                client.complete_text("system", "user")

        self.assertNotIn("010-1234-5678", str(raised.exception))
        self.assertIn("[NUMERIC_PERSONAL_DATA]", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_langsmith_trace_contains_only_sanitized_wrapper_result(self) -> None:
        model = _Model(AIMessage(content="결과 010-1234-5678"))
        captured: dict[str, object] = {}

        def fake_traceable(**kwargs: object):
            captured.update(kwargs)
            return lambda function: function

        with (
            patch.object(
                chat_completions_llm,
                "ChatOpenAI",
                return_value=model,
                create=True,
            ),
            patch.object(chat_completions_llm, "traceable", side_effect=fake_traceable),
            patch.object(chat_completions_llm, "langsmith_tracing_enabled", return_value=True),
            patch.object(
                chat_completions_llm,
                "tracing_context",
                return_value=nullcontext(),
                create=True,
            ) as tracing,
        ):
            client = ChatCompletionsJsonClient(
                ChatClientConfig(
                    api_key="provider-key",
                    model="gpt-5-nano",
                    provider="openai",
                )
            )
            result = client.complete_text("system", "user 010-9876-5432")

        self.assertEqual(result, "결과 [NUMERIC_PERSONAL_DATA]")
        self.assertEqual(
            captured["metadata"],
            {"provider": "openai", "model": "gpt-5-nano", "json_mode": False},
        )
        tracing.assert_called_once_with(enabled=False)


if __name__ == "__main__":
    unittest.main()
