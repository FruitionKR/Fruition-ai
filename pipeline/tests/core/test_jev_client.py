import json
import unittest
from unittest.mock import patch

import httpx

from app.core import jev_client
from app.core.jev_client import (
    JEV_MODEL,
    JevClient,
    JevUnavailable,
    build_jev_client,
    choice_probability,
)
from app.core.llm_prompt import LLM_SECURITY_BOUNDARY

QUESTIONS = {
    "q0": {
        "type": "choice",
        "instructions": "하나를 고르세요.",
        "criteria": {"include": "필요", "exclude": "불필요"},
    }
}


def _answer(choice: str = "include") -> dict:
    return {
        "model": JEV_MODEL,
        "answers": {"q0": {"type": "choice", "choice": choice, "confidence": 0.8, "probabilities": {choice: 0.8}}},
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


def _client(*responses: httpx.Response, requests: list[httpx.Request] | None = None) -> JevClient:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return queue.pop(0)

    return JevClient("key", http=httpx.Client(transport=httpx.MockTransport(handler)))


class BuildJevClientTest(unittest.TestCase):
    def test_returns_none_when_feature_is_off(self) -> None:
        with patch.dict("os.environ", {"JEV_ROUTING_ENABLED": "false", "TYPESAFE_API_KEY": "key"}):
            self.assertIsNone(build_jev_client("JEV_ROUTING_ENABLED"))

    def test_returns_none_without_api_key(self) -> None:
        with patch.dict("os.environ", {"JEV_ROUTING_ENABLED": "true", "TYPESAFE_API_KEY": " "}):
            self.assertIsNone(build_jev_client("JEV_ROUTING_ENABLED"))

    def test_builds_client_when_feature_is_on_and_key_exists(self) -> None:
        with patch.dict("os.environ", {"JEV_ROUTING_ENABLED": "true", "TYPESAFE_API_KEY": "key"}):
            self.assertIsInstance(build_jev_client("JEV_ROUTING_ENABLED"), JevClient)


class JevClientTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(jev_client, "_blocked_until", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_validated_answers_with_security_boundary_and_redaction(self) -> None:
        requests: list[httpx.Request] = []
        client = _client(httpx.Response(200, json=_answer()), requests=requests)

        answers = client.choose("연락처 010-1234-5678", QUESTIONS)

        self.assertEqual(answers["q0"]["choice"], "include")
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], JEV_MODEL)
        self.assertNotIn("010-1234-5678", body["state"])
        self.assertIn(LLM_SECURITY_BOUNDARY.strip(), body["questions"]["q0"]["instructions"])

    def test_credit_exhaustion_blocks_following_calls_without_request(self) -> None:
        requests: list[httpx.Request] = []
        client = _client(httpx.Response(402, json={"error": "no credits"}), requests=requests)

        with self.assertRaises(JevUnavailable):
            client.choose("state", QUESTIONS)
        with self.assertRaises(JevUnavailable):
            client.choose("state", QUESTIONS)

        self.assertEqual(len(requests), 1)

    def test_retries_rate_limit_once(self) -> None:
        client = _client(
            httpx.Response(429, headers={"retry-after": "0"}),
            httpx.Response(200, json=_answer("exclude")),
        )

        self.assertEqual(client.choose("state", QUESTIONS)["q0"]["choice"], "exclude")

    def test_repeated_server_error_is_unavailable(self) -> None:
        client = _client(
            httpx.Response(503, headers={"retry-after": "0"}),
            httpx.Response(503, headers={"retry-after": "0"}),
        )

        with self.assertRaises(JevUnavailable):
            client.choose("state", QUESTIONS)

    def test_choice_outside_criteria_is_unavailable(self) -> None:
        client = _client(httpx.Response(200, json=_answer("unknown")))

        with self.assertRaises(JevUnavailable):
            client.choose("state", QUESTIONS)

    def test_transport_error_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        client = JevClient("key", http=httpx.Client(transport=httpx.MockTransport(handler)))

        with self.assertRaises(JevUnavailable):
            client.choose("state", QUESTIONS)

    def test_choice_probability_ignores_invalid_values(self) -> None:
        self.assertEqual(choice_probability({"probabilities": {"include": 0.7}}, "include"), 0.7)
        self.assertEqual(choice_probability({"probabilities": {"include": 1.7}}, "include"), 0.0)
        self.assertEqual(choice_probability({}, "include"), 0.0)


if __name__ == "__main__":
    unittest.main()
