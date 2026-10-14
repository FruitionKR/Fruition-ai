import unittest
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage

from app.core.pipeline_control import PipelineRunCancelledError, task_cancellation_scope, task_run_id
from app.modules.model_usage.infrastructure import usage_ledger
from app.modules.wiki_generation.infrastructure import chat_completions_llm
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
    GenericChatCompletionsConceptPageGenerator,
    GenericChatCompletionsConceptResolver,
    GenericChatCompletionsExtractor,
    RetryOnceJsonCompletion,
    _complete_json_retrying_once,
)
from app.modules.wiki_generation.infrastructure.json_output_parser import JsonParseError


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


class _SequenceModel:
    """호출마다 준비된 응답을 차례로 돌려준다."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.calls = 0

    def invoke(self, messages: list[object]) -> AIMessage:
        self.calls += 1
        return AIMessage(content=self.contents.pop(0))


TRUNCATED_JSON = '{"notes": [{"title": "온실 환기",'


def _json_client(model: _SequenceModel) -> ChatCompletionsJsonClient:
    with patch.object(chat_completions_llm, "ChatGoogleGenerativeAI", return_value=model, create=True):
        return ChatCompletionsJsonClient(
            ChatClientConfig(api_key="provider-key", model="gemini-3.5-flash-lite", provider="gemini")
        )


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
                None,
            ),
            ("openai", "o4-mini", "ChatOpenAI", {"reasoning_effort": "medium"}, None),
            ("openai", "gpt-6-luna", "ChatOpenAI", {"reasoning_effort": "medium"}, None),
            ("openai", "gpt-4.1-mini", "ChatOpenAI", {}, 0.0),
            (
                "gemini",
                "gemini-3.1-flash-lite",
                "ChatGoogleGenerativeAI",
                {"thinking_level": "low"},
                0.0,
            ),
            ("claude", "claude-sonnet-5", "ChatAnthropic", {}, 0.0),
        )
        # OpenAI 추론 모델은 기본값(1) 외의 temperature를 거부하므로 호출자가 0.0을 줘도 보내지 않는다.
        for provider, model_name, class_name, provider_options, temperature in cases:
            with self.subTest(model=model_name):
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
                    temperature=temperature,
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
                    [block] = system_content
                    self.assertIn("Return only one valid JSON object", block["text"])
                    self.assertEqual(block["cache_control"], {"type": "ephemeral"})
                if provider != "claude":
                    self.assertIsInstance(system_content, str)

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


class CompleteJsonRetryTest(unittest.TestCase):
    def test_retries_once_when_first_response_is_truncated_json(self) -> None:
        ledger_calls: list[tuple[str, tuple]] = []

        @contextmanager
        def connect():
            yield SimpleNamespace(execute=lambda sql, params: ledger_calls.append((sql, params)))

        model = _SequenceModel([TRUNCATED_JSON, '{"notes": []}'])
        client = _json_client(model)
        token = task_run_id.set("run-1")
        try:
            with (
                patch.object(usage_ledger.database, "connect_ai", connect),
                usage_ledger.usage_scope({"run_id": "run-1", "workspace_id": "w", "user_id": "u", "kind": "ingest"}),
                self.assertLogs(chat_completions_llm.logger, "WARNING") as logs,
            ):
                result = _complete_json_retrying_once(client, "system", "user")
        finally:
            task_run_id.reset(token)

        self.assertEqual(result, {"notes": []})
        self.assertEqual(model.calls, 2)
        # 재요청도 공급사 호출이므로 같은 run에 귀속된 별도 원장 행으로 남는다.
        inserts = [params for sql, params in ledger_calls if "INSERT" in sql]
        updates = [params for sql, params in ledger_calls if "UPDATE" in sql]
        self.assertEqual(len(inserts), 2)
        self.assertNotEqual(inserts[0][0], inserts[1][0])
        self.assertEqual([row[1:5] for row in inserts], [("run-1", "w", "u", "ingest")] * 2)
        self.assertEqual([row[0] for row in updates], ["succeeded", "succeeded"])
        self.assertEqual(len(logs.records), 1)
        message = logs.records[0].getMessage()
        self.assertIn("run_id=run-1", message)
        self.assertIn("gemini-3.5-flash-lite", message)
        self.assertIn("line 1", message)
        self.assertNotIn("온실 환기", message)

    def test_raises_when_retry_is_also_invalid_json(self) -> None:
        model = _SequenceModel([TRUNCATED_JSON, TRUNCATED_JSON])
        client = _json_client(model)

        with self.assertLogs(chat_completions_llm.logger, "WARNING"), self.assertRaises(JsonParseError):
            _complete_json_retrying_once(client, "system", "user")

        self.assertEqual(model.calls, 2)

    def test_cancelled_task_does_not_retry(self) -> None:
        model = _SequenceModel([TRUNCATED_JSON, '{"notes": []}'])
        client = _json_client(model)
        active = iter([True, True, False])  # 첫 호출 전·후는 활성, 재요청 직전에 취소된다.

        with task_cancellation_scope(lambda: next(active)), self.assertRaises(PipelineRunCancelledError):
            _complete_json_retrying_once(client, "system", "user")

        self.assertEqual(model.calls, 1)

    def test_plain_complete_json_does_not_retry(self) -> None:
        # 자체 재요청 경로가 있는 호출자(markdown_edit, agent, meeting_notes 등)는 기존처럼 한 번만 묻는다.
        model = _SequenceModel([TRUNCATED_JSON, '{"notes": []}'])
        client = _json_client(model)

        with self.assertRaises(JsonParseError):
            client.complete_json("system", "user")

        self.assertEqual(model.calls, 1)

    def test_semantic_extractor_retries_truncated_json(self) -> None:
        model = _SequenceModel([TRUNCATED_JSON, '{"notes": []}'])
        client = _json_client(model)
        extractor = GenericChatCompletionsExtractor(client, "system")

        with (
            patch.object(chat_completions_llm, "render_semantic_user_prompt", return_value="user"),
            self.assertLogs(chat_completions_llm.logger, "WARNING"),
        ):
            result = extractor.extract(object())  # type: ignore[arg-type]

        self.assertEqual(result, {"notes": []})
        self.assertEqual(model.calls, 2)

    def test_concept_adapters_retry_truncated_json(self) -> None:
        cases = (
            ("render_concept_page_user_prompt", lambda c: GenericChatCompletionsConceptPageGenerator(c, "s").generate({}, [], [])),
            ("render_concept_resolution_user_prompt", lambda c: GenericChatCompletionsConceptResolver(c, "s").resolve([], [])),
        )
        for renderer, call in cases:
            with self.subTest(renderer=renderer):
                model = _SequenceModel([TRUNCATED_JSON, '{"pages": []}'])
                client = _json_client(model)
                with (
                    patch.object(chat_completions_llm, renderer, return_value="user"),
                    self.assertLogs(chat_completions_llm.logger, "WARNING"),
                ):
                    self.assertEqual(call(client), {"pages": []})
                self.assertEqual(model.calls, 2)

    def test_judge_candidates_retry_through_wrapper(self) -> None:
        from app.modules.wiki_generation.application.judge_candidates import judge_concept_update_candidates

        model = _SequenceModel([TRUNCATED_JSON, '{"decisions": []}'])
        completion = RetryOnceJsonCompletion(_json_client(model))

        with self.assertLogs(chat_completions_llm.logger, "WARNING"):
            result = judge_concept_update_candidates(
                completion=completion,
                concepts=[{"slug": "c-1", "title": "양액"}],
                candidates=[{"candidate_id": "cand_001", "term": "EC", "slug": "ec", "claim": "c", "refs": [], "candidate_type": "section"}],
            )

        self.assertEqual(result, [])
        self.assertEqual(model.calls, 2)


if __name__ == "__main__":
    unittest.main()
