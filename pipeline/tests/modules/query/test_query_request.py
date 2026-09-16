import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import api
from app.modules.query.domain.entities import GraphContext
from app.modules.query.interfaces.http.dependencies import get_query_answer_use_case
from app.modules.query.interfaces.http.schemas import QueryRequest


class QueryRequestTest(unittest.TestCase):
    def test_workspace_id_is_required(self) -> None:
        with self.assertRaises(ValidationError):
            QueryRequest(question="질문", provider="openai", model="gpt-5-nano", allow_web_search=False)

    def test_accepts_workspace_scoped_query(self) -> None:
        request = QueryRequest(
            workspace_id="ws_target",
            question="질문",
            provider="openai",
            model="gpt-5-nano",
            allow_web_search=False,
        )

        self.assertEqual(request.workspace_id, "ws_target")

    def test_accepts_response_preferences(self) -> None:
        request = QueryRequest(
            workspace_id="ws_target",
            question="질문",
            provider="openai",
            model="gpt-5-nano",
            output_language="en",
            response_length="concise",
            allow_web_search=False,
        )

        self.assertEqual(request.output_language, "en")
        self.assertEqual(request.response_length, "concise")
        self.assertFalse(request.allow_web_search)

    def test_rejects_unsupported_response_preference(self) -> None:
        with self.assertRaises(ValidationError):
            QueryRequest(
                workspace_id="ws_target",
                question="질문",
                provider="openai",
                model="gpt-5-nano",
                allow_web_search=False,
                output_language="fr",
            )

    def test_accepts_at_most_three_recent_conversation_pairs(self) -> None:
        request = QueryRequest(
            workspace_id="ws_target",
            question="후속 질문",
            provider="openai",
            model="gpt-5-nano",
            allow_web_search=False,
            recent_messages=[
                {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
                for index in range(6)
            ],
        )

        self.assertEqual(len(request.recent_messages), 6)
        with self.assertRaises(ValidationError):
            QueryRequest(
                workspace_id="ws_target",
                question="후속 질문",
                provider="openai",
                model="gpt-5-nano",
                allow_web_search=False,
                recent_messages=[
                    {"role": "user", "content": str(index)} for index in range(7)
                ],
            )

    def test_provider_and_model_are_required_and_non_empty(self) -> None:
        payloads = (
            {
                "workspace_id": "ws_target",
                "question": "질문",
                "provider": "openai",
                "allow_web_search": False,
            },
            {
                "workspace_id": "ws_target",
                "question": "질문",
                "provider": "openai",
                "model": "",
                "allow_web_search": False,
            },
            {
                "workspace_id": "ws_target",
                "question": "질문",
                "provider": "",
                "model": "gpt-5-nano",
                "allow_web_search": False,
            },
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    QueryRequest(**payload)

    def test_allow_web_search_rejects_non_boolean_values(self) -> None:
        for value in ("false", 0, 1):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    QueryRequest(
                        workspace_id="ws_target",
                        question="질문",
                        provider="openai",
                        model="gpt-5-nano",
                        allow_web_search=value,
                    )

    def test_query_route_propagates_provider_model_and_web_search(self) -> None:
        use_case = Mock()
        use_case.execute.return_value = Mock(
            answer=Mock(content="답변"),
            updated_conversation_summary=None,
            related_pages=[],
            evidence_snippets=[],
            graph_context=GraphContext(),
            traversal_paths=[],
            web_search=Mock(
                requested=False,
                executed=False,
                result_count=0,
                error_code=None,
            ),
        )

        with (
            patch.object(api.database, "ensure_ai_schema"),
            patch.dict("os.environ", {"INTERNAL_CALLBACK_TOKEN": "test-token"}),
            patch(
                "app.modules.query.interfaces.http.dependencies.build_answer_query_use_case",
                return_value=use_case,
            ) as build_use_case,
            TestClient(api.app) as client,
        ):
            response = client.post(
                "/query",
                json={
                    "workspace_id": "ws_target",
                    "question": "질문",
                    "provider": "gemini",
                    "model": "gemini-3.1-flash-lite",
                    "allow_web_search": True,
                },
                headers={"X-Internal-Token": "test-token"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        build_use_case.assert_called_once_with(
            provider="gemini",
            model="gemini-3.1-flash-lite",
            allow_web_search=True,
        )
        use_case.execute.assert_called_once_with(
            "질문",
            workspace_id="ws_target",
            user_id=None,
            conversation_context=None,
            allow_web_search=True,
        )

    def test_request_scoped_query_use_case_uses_payload_snapshot(self) -> None:
        payload = QueryRequest(
            workspace_id="ws_target",
            question="질문",
            provider="gemini",
            model="gemini-3.1-flash-lite",
            allow_web_search=True,
        )
        with patch(
            "app.modules.query.interfaces.http.dependencies.build_answer_query_use_case",
            return_value=Mock(),
        ) as build_use_case:
            get_query_answer_use_case(payload)

        build_use_case.assert_called_once_with(
            provider="gemini",
            model="gemini-3.1-flash-lite",
            allow_web_search=True,
        )


if __name__ == "__main__":
    unittest.main()
