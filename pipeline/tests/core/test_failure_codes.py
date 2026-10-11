import unittest

import httpx
from pydantic import BaseModel, ValidationError

from app.core.failure_codes import failure_code
from app.core.pipeline_control import DeadlineExceededError, PipelineRunCancelledError


class _Status(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("secret provider text")
        self.status_code = status_code


class RateLimitError(Exception):
    pass


class APITimeoutError(Exception):
    pass


def _coded():
    from app.modules.task_cancellation.infrastructure.postgres_task_journal import TaskFailedError

    return TaskFailedError("MEETING_NOTES_FAILED")


class FailureCodeTests(unittest.TestCase):
    def test_classifies_provider_and_input_errors(self) -> None:
        class M(BaseModel):
            a: int

        try:
            M(a="x")  # type: ignore[arg-type]
        except ValidationError as exc:
            validation = exc
        wrapped = RuntimeError("wrapper")
        wrapped.__cause__ = _Status(503)
        cases = [
            (_Status(429), "provider_rate_limited"),
            (RateLimitError("x"), "provider_rate_limited"),
            (_Status(503), "provider_unavailable"),
            (APITimeoutError("x"), "provider_unavailable"),
            (httpx.ConnectError("x"), "provider_unavailable"),
            (TimeoutError("x"), "provider_unavailable"),
            (wrapped, "provider_unavailable"),
            (_Status(422), "input_rejected"),
            (ValueError("x"), "input_rejected"),
            (validation, "input_rejected"),
            (DeadlineExceededError("x"), "deadline_exceeded"),
            (PipelineRunCancelledError("x"), "cancelled"),
            (_coded(), "MEETING_NOTES_FAILED"),
            (RuntimeError("secret"), "internal_error"),
        ]
        for exc, code in cases:
            with self.subTest(exc=type(exc).__name__, code=code):
                self.assertEqual(failure_code(exc), code)


class SdkErrorTests(unittest.TestCase):
    def test_real_sdk_errors_ignore_body_code(self) -> None:
        import json

        import openai
        from minio.error import S3Error

        request = httpx.Request("POST", "https://api.example.test")

        def sdk(cls, status: int, code: str):
            response = httpx.Response(status, request=request)
            return cls("secret", response=response, body={"code": code})

        cases = [
            (sdk(openai.RateLimitError, 429, "insufficient_quota"), "provider_rate_limited"),
            (sdk(openai.BadRequestError, 400, "context_length_exceeded"), "input_rejected"),
            (sdk(openai.AuthenticationError, 401, "invalid_api_key"), "internal_error"),
            (sdk(openai.InternalServerError, 500, "x"), "provider_unavailable"),
            (openai.APIConnectionError(request=request), "provider_unavailable"),
            (S3Error("NoSuchKey", "m", "r", "i", "h", None), "internal_error"),
            (httpx.HTTPStatusError("x", request=request, response=httpx.Response(503, request=request)), "provider_unavailable"),
            (json.JSONDecodeError("x", "", 0), "internal_error"),
        ]
        for exc, code in cases:
            with self.subTest(exc=type(exc).__name__, code=code):
                self.assertEqual(failure_code(exc), code)
