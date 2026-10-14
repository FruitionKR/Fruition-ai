import unittest
from pydantic import ValidationError

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


if __name__ == "__main__":
    unittest.main()
