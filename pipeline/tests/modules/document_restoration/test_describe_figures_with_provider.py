import argparse
import http.client
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from app.modules.document_restoration.infrastructure import describe_figures_with_provider as module

MODULE = "app.modules.document_restoration.infrastructure.describe_figures_with_provider"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00"
    b"\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def figure(index: int, size: float = 200.0) -> dict:
    return {
        "id": f"figure-{index}",
        "page": 1,
        "order": index,
        "type": "figure_candidate",
        "bbox": [10.0, 10.0, 10.0 + size, 10.0 + size],
        "asset": f"layout/crop_first/assets/figures/figure-{index}.png",
    }


class DescribeFiguresTest(unittest.TestCase):
    def _run(self, figures, outcomes, env=None):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        output_dir = Path(temp_dir.name)
        for block in figures:
            asset = output_dir / block["asset"]
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(PNG)
        manifest = [{"id": "body-1", "page": 1, "order": 0, "type": "paragraph"}, *figures]
        manifest_file = output_dir / "manifest.json"
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
        args = argparse.Namespace(
            manifest_file=manifest_file,
            output_dir=output_dir,
            provider="gemini",
            model="gemini-3.5-flash-lite",
            max_workers=1,
        )
        with mock.patch.dict(os.environ, env if env is not None else {"GEMINI_API_KEY": "k"}, clear=True), mock.patch(
            f"{MODULE}.call_page", side_effect=outcomes
        ) as call_page_mock:
            summary = module.run(args)
        return summary, call_page_mock, output_dir

    def test_describes_figure_with_repair_provider_and_bounded_call(self) -> None:
        summary, call_page_mock, output_dir = self._run(
            [figure(1)], [({"alt": "막대그래프", "description": "매출이 늘었다."}, {})]
        )

        call = call_page_mock.call_args.kwargs
        self.assertEqual((call["provider"], call["model"]), ("gemini", "gemini-3.5-flash-lite"))
        self.assertEqual(len(call["images"]), 1)
        self.assertTrue(call["images"][0].startswith("data:image/png;base64,"))
        self.assertEqual(call["schema"], module.FIGURE_SCHEMA)
        self.assertEqual(call["timeout"], module.CALL_TIMEOUT_SECONDS)
        self.assertEqual(call["max_output_tokens"], module.MAX_OUTPUT_TOKENS)
        self.assertIn("그림", call["prompt"])
        saved = json.loads(
            (output_dir / "layout" / "crop_first" / "figure_descriptions" / "figure-1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved, {"alt": "막대그래프", "description": "매출이 늘었다."})
        self.assertEqual(summary, {"requested": True, "described": 1, "failed": 0, "skipped": 0})
        self.assertEqual(
            json.loads((output_dir / "final" / "figure_descriptions_summary.json").read_text(encoding="utf-8")),
            summary,
        )

    def test_skips_tiny_figures_and_caps_count(self) -> None:
        figures = [figure(0, size=20.0)] + [figure(index) for index in range(1, module.MAX_FIGURES + 3)]
        reply = ({"alt": "그림", "description": "설명."}, {})
        summary, call_page_mock, _ = self._run(figures, [reply] * module.MAX_FIGURES)

        self.assertEqual(call_page_mock.call_count, module.MAX_FIGURES)
        self.assertEqual(summary, {"requested": True, "described": module.MAX_FIGURES, "failed": 0, "skipped": 3})

    def test_failures_leave_figure_without_description_and_are_counted(self) -> None:
        summary, _, output_dir = self._run(
            [figure(1), figure(2), figure(3)],
            [
                urllib.error.URLError("down"),
                TimeoutError("slow"),
                ({"alt": "", "description": "빈 alt"}, {}),
            ],
        )

        self.assertEqual(summary, {"requested": True, "described": 0, "failed": 3, "skipped": 0})
        self.assertFalse((output_dir / "layout" / "crop_first" / "figure_descriptions").exists())

    def test_transport_error_fails_only_that_figure(self) -> None:
        summary, _, output_dir = self._run(
            [figure(1), figure(2)],
            [
                http.client.IncompleteRead(b"partial"),
                ({"alt": "막대그래프", "description": "매출이 늘었다."}, {}),
            ],
        )

        self.assertEqual(summary, {"requested": True, "described": 1, "failed": 1, "skipped": 0})
        self.assertTrue((output_dir / "layout" / "crop_first" / "figure_descriptions" / "figure-2.json").exists())

    def test_missing_api_key_sends_nothing_and_counts_failures(self) -> None:
        summary, call_page_mock, _ = self._run([figure(1)], [], env={})
        call_page_mock.assert_not_called()
        self.assertEqual(summary, {"requested": True, "described": 0, "failed": 1, "skipped": 0})

    def test_clean_description_keeps_markdown_intact_and_limits_length(self) -> None:
        cleaned = module.clean_description(
            {"alt": "[그림](x) " + "가" * 100, "description": "첫 줄\n\n> 둘째 줄"}
        )
        self.assertNotRegex(cleaned["alt"], r"[\[\]()!\n]")
        self.assertLessEqual(len(cleaned["alt"]), module.MAX_ALT_LENGTH)
        self.assertNotIn("\n", cleaned["description"])


if __name__ == "__main__":
    unittest.main()
