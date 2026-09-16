import argparse
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from app.modules.document_restoration.infrastructure.crop_first_with_anydoc import (
    assemble,
)
from app.modules.document_restoration.infrastructure.selective_repair_with_provider import (
    OUTPUT_SCHEMA,
    block_markdown,
    call_page,
    clean_previous_results,
    markdown_fragments,
    normalize_replacement,
    page_markdown,
    openai_response_text,
    run,
    save_replacements,
    select_candidates,
    valid_replacement,
)

MODULE = (
    "app.modules.document_restoration.infrastructure."
    "selective_repair_with_provider"
)


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class SelectiveRepairWithProviderTest(unittest.TestCase):
    def test_selects_all_tables_equations_and_only_damaged_text(self) -> None:
        blocks = [
            {"id": "table", "type": "table_candidate"},
            {"id": "equation", "type": "equation_candidate"},
            {"id": "normal", "type": "paragraph", "source_text": "Normal text"},
            {
                "id": "damaged",
                "type": "paragraph",
                "source_text": "Normal text",
                "text_decision": "needs_text_adjudication",
            },
        ]

        self.assertEqual(
            [block["id"] for block in select_candidates(blocks)],
            ["table", "equation", "damaged"],
        )

    def test_extracts_fragments_and_pages_from_detected_markdown(self) -> None:
        markdown = """## Page 1

<!-- docling_text_p01_001 type=paragraph bbox=[1, 2, 3, 4] confidence=x -->
First

## Page 2

<!-- docling_text_p02_001 type=paragraph bbox=[1, 2, 3, 4] confidence=x -->
Second
"""

        self.assertEqual(
            markdown_fragments(markdown),
            {
                "docling_text_p01_001": "First",
                "docling_text_p02_001": "Second",
            },
        )
        self.assertIn("First", page_markdown(markdown)[1])
        self.assertIn("Second", page_markdown(markdown)[2])

    def test_validates_replacement_by_block_type(self) -> None:
        self.assertTrue(valid_replacement("equation_candidate", "$$\nx_{1}=1\n$$"))
        self.assertTrue(valid_replacement("equation_candidate", r"\[x_{1}=1\]"))
        self.assertFalse(valid_replacement("equation_candidate", "$$x_{1}=1"))
        self.assertTrue(
            valid_replacement(
                "table_candidate",
                "| A | B |\n| --- | --- |\n| 1 | 2 |",
            )
        )
        self.assertFalse(valid_replacement("heading", "Heading"))
        self.assertTrue(valid_replacement("heading", "## Heading"))
        self.assertFalse(
            valid_replacement("heading", "## Heading\nUnexpected body")
        )
        self.assertFalse(
            valid_replacement(
                "paragraph",
                "XQ001QX",
                ["XQ001QX"],
                scope="page_body",
                source_text="긴 원문 본문 " * 20 + "XQ001QX",
            )
        )
        self.assertEqual(
            normalize_replacement("equation_candidate", r"\[x_{1}=1\]"),
            "$$\nx_{1}=1\n$$",
        )

    def test_uses_richest_body_candidate_for_prompt_and_validation(self) -> None:
        block = {
            "id": "body-1",
            "markdown": "XQ001QX",
            "source_text": "XQ001QX",
            "fallback_text": "완전한 원본 본문 " * 10,
        }

        self.assertEqual(
            block_markdown(block, {"body-1": "XQ001QX"}),
            block["fallback_text"],
        )

    def test_rejects_token_only_detected_source_against_long_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            block = {
                "id": "body-1",
                "type": "paragraph",
                "scope": "page_body",
                "markdown": "XQ001QX",
                "source_text": "XQ001QX",
                "fallback_text": "완전한 원본 본문 " * 10,
                "required_tokens": ["XQ001QX"],
            }

            counts = save_replacements(
                Path(temp_dir),
                [block],
                {
                    "results": [
                        {
                            "block_id": "body-1",
                            "action": "replace",
                            "replacement": "XQ001QX",
                        }
                    ]
                },
                "openai",
                {"body-1": "XQ001QX"},
            )

            self.assertEqual(
                counts,
                {"replace": 0, "keep": 0, "rejected": 1},
            )

    def test_allows_token_only_true_table_page_without_fallback(self) -> None:
        block = {
            "id": "body-1",
            "type": "paragraph",
            "scope": "page_body",
            "markdown": "XQ001QX",
            "source_text": "XQ001QX",
            "fallback_text": "",
            "required_tokens": ["XQ001QX"],
        }

        self.assertTrue(
            valid_replacement(
                block["type"],
                "XQ001QX",
                block["required_tokens"],
                scope=block["scope"],
                source_text=block_markdown(block, {"body-1": "XQ001QX"}),
            )
        )

    def test_extracts_structured_output_text(self) -> None:
        payload = {"results": []}
        response = {
            "output": [
                {"type": "reasoning"},
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(payload),
                        }
                    ],
                },
            ]
        }

        self.assertEqual(json.loads(openai_response_text(response)), payload)

    def test_calls_responses_api_with_images_and_json_schema(self) -> None:
        response_body = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"results":[]}',
                        }
                    ],
                }
            ],
            "usage": {"total_tokens": 10},
        }

        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(response_body),
        ) as urlopen:
            result, usage = call_page(
                provider="openai",
                api_key="test-key",
                model="gpt-5-nano",
                prompt="restore",
                payload={"blocks": []},
                images=["data:image/png;base64,AA=="],
            )

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "gpt-5-nano")
        self.assertEqual(body["reasoning"], {"effort": "medium"})
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertEqual(
            body["input"][1]["content"][1]["type"],
            "input_image",
        )
        self.assertEqual(result, {"results": []})
        self.assertEqual(usage, {"total_tokens": 10})

    def test_calls_gemini_api_with_inline_images_and_json_schema(self) -> None:
        response_body = {
            "candidates": [
                {"content": {"parts": [{"text": '{"results":[]}' }]}}
            ],
            "usageMetadata": {"totalTokenCount": 12},
        }
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(response_body),
        ) as urlopen:
            result, usage = call_page(
                provider="gemini",
                api_key="test-key",
                model="gemini-3.1-flash-lite",
                prompt="restore",
                payload={"blocks": []},
                images=["data:image/png;base64,AA=="],
            )

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertIn("gemini-3.1-flash-lite:generateContent", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "test-key")
        self.assertEqual(
            body["contents"][0]["parts"][1]["inline_data"],
            {"mime_type": "image/png", "data": "AA=="},
        )
        self.assertEqual(
            body["generationConfig"]["responseJsonSchema"], OUTPUT_SCHEMA
        )
        self.assertEqual(result, {"results": []})
        self.assertEqual(usage, {"totalTokenCount": 12})

    def test_calls_claude_api_with_base64_images_and_json_schema(self) -> None:
        response_body = {
            "content": [{"type": "text", "text": '{"results":[]}'}],
            "usage": {"input_tokens": 8, "output_tokens": 4},
        }
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(response_body),
        ) as urlopen:
            result, usage = call_page(
                provider="claude",
                api_key="test-key",
                model="claude-sonnet-5",
                prompt="restore",
                payload={"blocks": []},
                images=["data:image/png;base64,AA=="],
            )

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), "test-key")
        self.assertEqual(
            body["messages"][0]["content"][0]["source"],
            {"type": "base64", "media_type": "image/png", "data": "AA=="},
        )
        self.assertEqual(
            body["output_config"]["format"]["schema"], OUTPUT_SCHEMA
        )
        self.assertEqual(result, {"results": []})
        self.assertEqual(usage, {"input_tokens": 8, "output_tokens": 4})

    def test_does_not_expose_responses_api_error_body(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.openai.test/v1/responses",
            400,
            "Bad Request",
            {},
            None,
        )
        error.read = mock.Mock(return_value=b"sensitive document text")

        with (
            mock.patch("urllib.request.urlopen", side_effect=error),
            self.assertRaisesRegex(RuntimeError, r"^OpenAI Responses API HTTP 400$"),
        ):
            call_page(
                provider="openai",
                api_key="test-key",
                model="gpt-5-nano",
                prompt="restore",
                payload={"blocks": []},
                images=[],
            )

        error.read.assert_not_called()

    def test_propagates_retryable_responses_api_errors_for_fallback(self) -> None:
        for status_code in (429, 500):
            error = urllib.error.HTTPError(
                "https://api.openai.test/v1/responses",
                status_code,
                "Retry",
                {},
                None,
            )
            with self.subTest(status_code=status_code), (
                mock.patch("urllib.request.urlopen", side_effect=error)
            ), self.assertRaises(urllib.error.HTTPError) as raised:
                call_page(
                    provider="openai",
                    api_key="test-key",
                    model="gpt-5-nano",
                    prompt="restore",
                    payload={"blocks": []},
                    images=[],
                )

            self.assertEqual(raised.exception.code, status_code)

    def test_skips_openai_without_api_key_and_keeps_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            manifest_file = output_dir / "manifest.json"
            detected_markdown = output_dir / "detected.md"
            manifest_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "body-1",
                            "page": 1,
                            "order": 0,
                            "type": "paragraph",
                            "source_text": "before XQ001QX after",
                            "body_broken": False,
                        },
                        {
                            "id": "table-1",
                            "page": 1,
                            "order": 1,
                            "type": "table_candidate",
                            "token": "XQ001QX",
                            "asset": "layout/crop_first/assets/specials/table.png",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            asset = (
                output_dir
                / "layout"
                / "crop_first"
                / "assets"
                / "specials"
                / "table.png"
            )
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"png")
            detected_markdown.write_text("baseline", encoding="utf-8")
            args = argparse.Namespace(
                pdf_file=output_dir / "input.pdf",
                manifest_file=manifest_file,
                detected_markdown=detected_markdown,
                output_dir=output_dir,
                provider="openai",
                model="gpt-5-nano",
                max_workers=1,
            )

            with mock.patch.dict(
                os.environ,
                {"OPENAI_API_KEY": ""},
            ), mock.patch(f"{MODULE}.call_page") as call_page_mock:
                summary = run(args)

            self.assertEqual(summary["calls"], 0)
            self.assertEqual(summary["blocks"], 1)
            call_page_mock.assert_not_called()
            output_file = output_dir / "final" / "restored.md"
            assemble(manifest_file, output_dir, output_file)
            self.assertIn(
                "[source crop](../layout/crop_first/assets/specials/table.png)",
                output_file.read_text(encoding="utf-8"),
            )

    def test_uses_only_selected_provider_key_and_keeps_baseline_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            manifest_file = output_dir / "manifest.json"
            detected_markdown = output_dir / "detected.md"
            manifest_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "table-1",
                            "page": 1,
                            "order": 1,
                            "type": "table_candidate",
                            "bbox": [0, 0, 1, 1],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            detected_markdown.write_text("## Page 1\n", encoding="utf-8")
            args = argparse.Namespace(
                pdf_file=output_dir / "input.pdf",
                manifest_file=manifest_file,
                detected_markdown=detected_markdown,
                output_dir=output_dir,
                provider="gemini",
                model="gemini-3.1-flash-lite",
                max_workers=1,
            )
            error = urllib.error.HTTPError(
                "https://gemini.test", 429, "Retry", {}, None
            )

            with mock.patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "openai-key",
                    "GEMINI_API_KEY": "gemini-key",
                    "ANTHROPIC_API_KEY": "claude-key",
                },
                clear=True,
            ), mock.patch(
                f"{MODULE}.render_page",
                return_value="page",
            ), mock.patch(
                f"{MODULE}.block_image",
                return_value="crop",
            ), mock.patch(
                f"{MODULE}.call_page",
                side_effect=error,
            ) as call_page_mock:
                summary = run(args)

            self.assertEqual(call_page_mock.call_count, 2)
            for call in call_page_mock.call_args_list:
                self.assertEqual(call.kwargs["provider"], "gemini")
                self.assertEqual(call.kwargs["api_key"], "gemini-key")
            self.assertEqual(summary["pages"][0]["failed"], 1)
            self.assertEqual(summary["pages"][0]["batch_error"], "HTTPError")

    def test_rejects_invalid_replacement_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            blocks = [
                {
                    "id": "docling_formula_p01_001",
                    "type": "equation_candidate",
                }
            ]

            counts = save_replacements(
                output_dir,
                blocks,
                {
                    "results": [
                        {
                            "block_id": "docling_formula_p01_001",
                            "action": "replace",
                            "replacement": "broken",
                        }
                    ]
                },
                "openai",
            )

            self.assertEqual(counts, {"replace": 0, "keep": 0, "rejected": 1})
            self.assertFalse(
                (
                    output_dir
                    / "layout"
                    / "auto"
                    / "recovered_blocks"
                    / "docling_formula_p01_001.md"
                ).exists()
            )

    def test_cleans_all_results_from_previous_restoration_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            evaluation_dir = output_dir / "layout" / "auto" / "evaluations"
            recovered_dir = (
                output_dir / "layout" / "auto" / "recovered_blocks"
            )
            evaluation_dir.mkdir(parents=True)
            recovered_dir.mkdir(parents=True)
            stale_evaluation = evaluation_dir / "stale.json"
            stale_recovered = recovered_dir / "stale.md"
            preserved_evaluation = evaluation_dir / "preserved.json"
            stale_evaluation.write_text(
                json.dumps(
                    {"recovery_source": "openai_selective_repair"}
                ),
                encoding="utf-8",
            )
            stale_recovered.write_text("stale", encoding="utf-8")
            preserved_evaluation.write_text(
                json.dumps({"recovery_source": "local_ocr"}),
                encoding="utf-8",
            )
            preserved_recovered = recovered_dir / "preserved.md"
            preserved_recovered.write_text("local result", encoding="utf-8")

            clean_previous_results(output_dir)

            self.assertFalse(stale_evaluation.exists())
            self.assertFalse(stale_recovered.exists())
            self.assertFalse(preserved_evaluation.exists())
            self.assertFalse(preserved_recovered.exists())


if __name__ == "__main__":
    unittest.main()
