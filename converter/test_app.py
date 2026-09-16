import base64
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

PIPELINE_ROOT = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_ROOT))
SPEC = importlib.util.spec_from_file_location(
    "converter_app", Path(__file__).with_name("app.py")
)
assert SPEC is not None and SPEC.loader is not None
converter_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(converter_app)
process_pdf = converter_app.process_pdf


class ConverterCropFirstBoundaryTest(unittest.TestCase):
    def test_converter_image_exposes_pipeline_module_path(self) -> None:
        dockerfile = Path(__file__).with_name("Dockerfile")

        self.assertIn("ENV PYTHONPATH=/app\n", dockerfile.read_text(encoding="utf-8"))

    def test_process_pdf_preserves_crop_asset_link_in_markdown(self) -> None:
        fixture = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00"
            b"\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        asset_paths: list[Path] = []

        def fake_run_to_file(
            command: list[str],
            output_file: Path,
            working_dir: Path,
            timeout_seconds: int,
            log_file: Path,
            cancelled=None,
        ) -> None:
            output_file.write_text("diagnostic", encoding="utf-8")
            log_file.touch()

        def fake_run(
            command: list[str],
            working_dir: Path,
            timeout_seconds: int,
            log_file: Path,
            cancelled=None,
        ) -> None:
            output_dir = Path(command[command.index("--output-dir") + 1])
            slug = command[command.index("--document-slug") + 1]
            asset = output_dir / "layout" / "crop_first" / "assets" / "figures" / "region.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(fixture)
            asset_paths.append(asset)
            output_file = output_dir / "final" / f"{slug}.restored.md"
            output_file.parent.mkdir(parents=True)
            output_file.write_text(
                "![figure](../layout/crop_first/assets/figures/region.png)\n",
                encoding="utf-8",
            )
            (output_dir / "final" / "selective_repair_summary.json").write_text(
                '{"provider":"gemini","calls":1}', encoding="utf-8"
            )

        with mock.patch.object(converter_app, "missing_commands", return_value=[]):
            with mock.patch.object(
                converter_app, "run_to_file", side_effect=fake_run_to_file
            ):
                with mock.patch.object(
                    converter_app, "run", side_effect=fake_run
                ) as restoration:
                    result = process_pdf(b"pdf")

        command = restoration.call_args.args[0]
        self.assertEqual(command[command.index("--mode") + 1], "crop-first")
        self.assertEqual(
            command[command.index("--selective-provider") + 1], "gemini"
        )
        self.assertEqual(
            command[command.index("--selective-model") + 1],
            "gemini-3.1-flash-lite",
        )
        self.assertEqual(result["repair_summary"]["provider"], "gemini")
        self.assertFalse(asset_paths[0].exists())
        marker = "data:image/png;base64,"
        self.assertIn(marker, result["markdown"])
        encoded = result["markdown"].split(marker, 1)[1].split(")", 1)[0]
        self.assertEqual(base64.b64decode(encoded), fixture)


class ConverterCancellationTest(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_waits_for_worker_cleanup(self):
        import asyncio
        import io
        from threading import Event
        from fastapi import UploadFile, HTTPException
        from starlette.datastructures import Headers
        cleaned = Event()
        request = mock.Mock()
        request.is_disconnected = mock.AsyncMock(return_value=True)

        def convert(content, provider, model, cancelled):
            try:
                if not cancelled.wait(2):
                    raise AssertionError("취소 신호가 전달되지 않았습니다.")
                raise HTTPException(499, "Conversion cancelled")
            finally:
                cleaned.set()

        upload = UploadFile(io.BytesIO(b"pdf"), filename="test.pdf", headers=Headers({"content-type": "application/pdf"}))
        with mock.patch.object(converter_app, "process_pdf", side_effect=convert):
            with self.assertRaises(HTTPException) as raised:
                await asyncio.wait_for(converter_app.convert(request, upload, "gemini", "test"), 3)
        self.assertEqual(raised.exception.status_code, 499)
        self.assertTrue(cleaned.is_set())

    def test_cancellation_terminates_running_process(self):
        import tempfile
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from fastapi import HTTPException
        cancelled = Event()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with ThreadPoolExecutor(max_workers=1) as executor:
                started = root / "started"
                future = executor.submit(converter_app.run, [sys.executable, "-c", "from pathlib import Path; import os,time; Path('started').write_text(str(os.getpid())); time.sleep(60)"],
                                         root, 30, root / "log", cancelled)
                import time, os
                deadline = time.monotonic() + 3
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(started.exists())
                process_id = int(started.read_text())
                cancelled.set()
                with self.assertRaises(HTTPException) as raised:
                    future.result(timeout=3)
                self.assertEqual(raised.exception.status_code, 499)
                with self.assertRaises(ProcessLookupError):
                    os.kill(process_id, 0)


if __name__ == "__main__":
    unittest.main()
