import argparse
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from app.core.llm_env import DEFAULT_LLM_MODELS

from app.modules.document_restoration.infrastructure.selective_repair_with_provider import (
    REPAIR_MAX_OUTPUT_TOKENS,
    call_page,
    readable_length,
    run,
    valid_replacement,
)

MODULE = "app.modules.document_restoration.infrastructure.selective_repair_with_provider"
SOURCE_SENTENCE = "가나다라마바사아자차"  # 읽을 수 있는 글자 10자


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def body_valid(replacement: str, source: str) -> bool:
    return valid_replacement("paragraph", replacement, [], scope="page_body", source_text=source)


class PageBodyLengthThresholdTest(unittest.TestCase):
    def test_readable_length_ignores_whitespace_and_garbage_characters(self) -> None:
        self.assertEqual(readable_length("가 나\n다�\x07​"), 3)

    def test_garbage_in_source_does_not_inflate_the_length_requirement(self) -> None:
        # 글꼴 매핑이 없는 쪽: 읽을 수 있는 글자 100자에 깨진 글자 200자가 섞였다.
        source = SOURCE_SENTENCE * 10 + "�" * 120 + "" * 80
        # 이전 기준(공백만 뺀 원문 300자의 50%=150자)이면 버려지던 온전한 복원(100자)을 받는다.
        self.assertTrue(body_valid(SOURCE_SENTENCE * 10, source))

    def test_moderately_shorter_repair_is_accepted(self) -> None:
        # 머리말·중복 공백 정리로 45% 수준까지 줄어든 복원은 받는다(이전 50% 기준이면 거부).
        self.assertTrue(body_valid(SOURCE_SENTENCE * 9, SOURCE_SENTENCE * 20))

    def test_repair_that_drops_most_of_the_text_is_still_rejected(self) -> None:
        self.assertFalse(body_valid(SOURCE_SENTENCE * 7, SOURCE_SENTENCE * 20))
        self.assertFalse(body_valid(SOURCE_SENTENCE * 3, SOURCE_SENTENCE * 10 + "�" * 100))

    def test_required_tokens_are_still_enforced(self) -> None:
        self.assertFalse(
            valid_replacement(
                "paragraph", SOURCE_SENTENCE * 10, ["XQ001QX"], scope="page_body", source_text=SOURCE_SENTENCE * 10
            )
        )


class RepairOutputLimitTest(unittest.TestCase):
    RESPONSES = {
        "openai": {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"results":[]}'}]}]},
        "gemini": {"candidates": [{"content": {"parts": [{"text": '{"results":[]}'}]}}]},
        "claude": {"content": [{"type": "text", "text": '{"results":[]}'}]},
    }

    def test_every_provider_call_sets_an_explicit_output_token_limit(self) -> None:
        for provider, response in self.RESPONSES.items():
            with self.subTest(provider=provider), mock.patch(
                "urllib.request.urlopen", return_value=_FakeResponse(response)
            ) as urlopen:
                call_page(provider=provider, api_key="k", model=DEFAULT_LLM_MODELS[provider], prompt="p", payload={"blocks": []}, images=[])
            body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
            limit = {
                "openai": lambda: body["max_output_tokens"],
                "gemini": lambda: body["generationConfig"]["maxOutputTokens"],
                "claude": lambda: body["max_tokens"],
            }[provider]()
            self.assertEqual(limit, REPAIR_MAX_OUTPUT_TOKENS)
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 180)


class BrokenPageRetryTest(unittest.TestCase):
    """깨진 쪽 본문은 거부·실패하면 그 쪽만 한 번 더 묻고, 그래도 안 되면 까닭을 요약에 남긴다."""

    def _run(self, outcomes):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        output_dir = Path(temp_dir.name)
        manifest_file = output_dir / "manifest.json"
        body = {
            "id": "body-1",
            "page": 1,
            "order": 0,
            "type": "paragraph",
            "scope": "page_body",
            "bbox": [0, 0, 100, 100],
            "source_text": SOURCE_SENTENCE * 10,
            "fallback_text": SOURCE_SENTENCE * 10,
            "required_tokens": [],
            "replacement_required": True,
            "body_broken": True,
            "text_decision": "needs_text_adjudication",
        }
        healthy = {**body, "id": "body-2", "page": 2, "replacement_required": False, "body_broken": False}
        healthy.pop("text_decision")
        manifest_file.write_text(json.dumps([body, healthy], ensure_ascii=False), encoding="utf-8")
        detected = output_dir / "detected.md"
        detected.write_text("## Page 1\n", encoding="utf-8")
        args = argparse.Namespace(
            pdf_file=output_dir / "input.pdf",
            manifest_file=manifest_file,
            detected_markdown=detected,
            output_dir=output_dir,
            provider="gemini",
            model="gemini-3.5-flash-lite",
            max_workers=1,
        )
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=True), mock.patch(
            f"{MODULE}.block_image", return_value="crop"
        ), mock.patch(f"{MODULE}.call_page", side_effect=outcomes) as call_page_mock:
            summary = run(args)
        return summary, call_page_mock, output_dir

    @staticmethod
    def _reply(replacement):
        return {"results": [{"block_id": "body-1", "action": "replace", "replacement": replacement}]}, {}

    def test_rejected_page_is_retried_once_and_accepted(self) -> None:
        summary, call_page_mock, output_dir = self._run([self._reply("짧음"), self._reply(SOURCE_SENTENCE * 10)])
        self.assertEqual(call_page_mock.call_count, 2)
        self.assertTrue((output_dir / "layout" / "auto" / "text_recovered" / "body-1.md").exists())
        self.assertEqual(summary["pages"][0]["failures"], {})

    def test_twice_rejected_page_stops_after_one_retry_and_records_reason(self) -> None:
        summary, call_page_mock, _ = self._run([self._reply("짧음"), self._reply("짧음")])
        # 한 번만 다시 묻고, 정상 쪽(body-2)은 묻지 않는다.
        self.assertEqual(call_page_mock.call_count, 2)
        self.assertEqual(summary["pages"][0]["failures"], {"body-1": "repair_rejected"})

    def test_provider_error_is_retried_once_and_recorded(self) -> None:
        error = urllib.error.URLError("down")
        summary, call_page_mock, _ = self._run([error, error])
        self.assertEqual(call_page_mock.call_count, 2)
        self.assertEqual(summary["pages"][0]["failures"], {"body-1": "provider_error"})

    def test_successful_first_attempt_is_not_retried(self) -> None:
        summary, call_page_mock, _ = self._run([self._reply(SOURCE_SENTENCE * 10)])
        self.assertEqual(call_page_mock.call_count, 1)
        self.assertEqual(summary["pages"][0]["failures"], {})


if __name__ == "__main__":
    unittest.main()
