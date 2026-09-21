import base64
import importlib.util
import json
import sys
import tempfile
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


CHECK_SPEC = importlib.util.spec_from_file_location(
    "check_cpu_only", Path(__file__).with_name("check_cpu_only.py")
)
assert CHECK_SPEC is not None and CHECK_SPEC.loader is not None
check_cpu_only = importlib.util.module_from_spec(CHECK_SPEC)
CHECK_SPEC.loader.exec_module(check_cpu_only)


class ConverterCpuOnlyImageTest(unittest.TestCase):
    def test_requirements_pin_cpu_torch_wheels_from_official_index(self) -> None:
        requirements = Path(__file__).with_name("requirements.txt").read_text(encoding="utf-8")

        self.assertIn("--extra-index-url https://download.pytorch.org/whl/cpu\n", requirements)
        self.assertRegex(requirements, r"(?m)^torch==\d+\.\d+\.\d+\+cpu$")
        self.assertRegex(requirements, r"(?m)^torchvision==\d+\.\d+\.\d+\+cpu$")

    def test_dockerfile_runs_cpu_only_check_after_pip_install(self) -> None:
        dockerfile = Path(__file__).with_name("Dockerfile").read_text(encoding="utf-8")

        self.assertIn("check_cpu_only.py", dockerfile)
        self.assertLess(dockerfile.index("pip install"), dockerfile.index("python check_cpu_only.py"))

    def test_check_accepts_cpu_wheels_without_gpu_packages(self) -> None:
        errors = check_cpu_only.check_versions(
            {"torch": "2.14.0+cpu", "torchvision": "0.29.0+cpu", "docling": "2.129.0"}
        )

        self.assertEqual(errors, [])

    def test_check_rejects_pypi_torch_and_nvidia_packages(self) -> None:
        errors = check_cpu_only.check_versions(
            {
                "torch": "2.14.0",
                "torchvision": "0.29.0+cpu",
                "nvidia-cudnn-cu13": "9.24.0.43",
                "cuda-toolkit": "13.0.3",
                "triton": "3.8.0",
            }
        )

        self.assertEqual(
            errors,
            [
                "torch==2.14.0 is not a +cpu wheel",
                "GPU packages present: cuda-toolkit, nvidia-cudnn-cu13, triton",
            ],
        )

    def test_check_reads_pip_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            report.write_text(
                json.dumps(
                    {
                        "install": [
                            {"metadata": {"name": "Torch", "version": "2.14.0+cpu"}},
                            {"metadata": {"name": "torchvision", "version": "0.29.0+cpu"}},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(check_cpu_only.main(["--report", str(report)]), 0)

            report.write_text(
                json.dumps({"install": [{"metadata": {"name": "torch", "version": "2.14.0"}}]}),
                encoding="utf-8",
            )

            self.assertEqual(check_cpu_only.main(["--report", str(report)]), 1)


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


class LargePdfRangeBatchTest(unittest.TestCase):
    def reader(self, size):
        from threading import Event
        with mock.patch.dict("os.environ", {"CONVERTER_SOURCE_HOSTS": "storage.example.test"}):
            return converter_app.S3RangeReader("https://storage.example.test/object?signature=private", size, Event())

    def test_more_than_2gib_seek_fetches_only_one_range(self):
        import io
        size = 3 * 1024**3
        reader = self.reader(size)
        requests = []
        class Response(io.BytesIO):
            status = 206
        def open_range(request, timeout):
            first, last = map(int, request.get_header("Range").removeprefix("bytes=").split("-"))
            requests.append((first, last))
            response = Response(b"x" * (last - first + 1))
            response.headers = {"Content-Range": f"bytes {first}-{last}/{size}"}
            return response
        reader.opener.open = open_range
        reader.seek(-12, 2)
        self.assertEqual(reader.read(12), b"x" * 12)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][1] - requests[0][0] + 1, 1024**2)
        reader.seek(-6, 2)
        self.assertEqual(reader.read(6), b"x" * 6)
        self.assertEqual(len(requests), 1)

    def test_unapproved_host_and_ignored_range_are_rejected(self):
        from threading import Event
        with self.assertRaises(ValueError):
            converter_app.S3RangeReader("http://169.254.169.254/latest/meta-data", 100, Event())
        reader = self.reader(100)
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        reader.opener.open = mock.Mock(return_value=response)
        with self.assertRaisesRegex(ValueError, "Could not read"):
            reader.read(1)

    def test_page_batches_resume_without_reconverting_previous_pages(self):
        import io
        from threading import Event
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        for _ in range(23): writer.add_blank_page(width=72, height=72)
        raw = io.BytesIO(); writer.write(raw)
        def convert(path, *args):
            self.assertIsInstance(path, Path)
            return {"markdown": f"pages={len(PdfReader(path).pages)}"}
        with mock.patch.object(converter_app, "S3RangeReader", side_effect=lambda *args: io.BytesIO(raw.getvalue())), \
             mock.patch.object(converter_app, "process_pdf", side_effect=convert), \
             mock.patch.dict("os.environ", {"PDF_PAGES_PER_BATCH": "10"}):
            first = converter_app.process_source_batch("signed", len(raw.getvalue()), 10, "gemini", "model", Event())
            last = converter_app.process_source_batch("signed", len(raw.getvalue()), 20, "gemini", "model", Event())
        self.assertEqual((first["page_start"], first["page_end"], first["markdown"], first["done"]), (11, 20, "pages=10", False))
        self.assertEqual((last["page_start"], last["page_end"], last["markdown"], last["done"]), (21, 23, "pages=3", True))


class SparseLargePdfTest(unittest.TestCase):
    def test_valid_three_gib_pdf_is_parsed_using_only_remote_ranges(self):
        import io, re
        from threading import Event
        from pypdf import PdfWriter, PdfReader
        writer = PdfWriter()
        for _ in range(12): writer.add_blank_page(width=72, height=72)
        output = io.BytesIO(); writer.write(output); raw = output.getvalue()
        split = raw.index(b"\nxref\n") + 1
        prefix = raw[:split]
        xref_offset = 3 * 1024**3
        suffix = re.sub(rb"startxref\n[0-9]+", f"startxref\n{xref_offset}".encode(), raw[split:])
        size = xref_offset + len(suffix)
        fetched = []
        class Response(io.BytesIO): status = 206
        def open_range(request, timeout):
            first, last = map(int, request.get_header("Range").removeprefix("bytes=").split("-"))
            data = bytearray(b" " * (last - first + 1))
            for start, value in [(0, prefix), (xref_offset, suffix)]:
                lo, hi = max(start, first), min(start + len(value), last + 1)
                if lo < hi: data[lo-first:hi-first] = value[lo-start:hi-start]
            fetched.append(len(data))
            response = Response(data); response.headers = {"Content-Range": f"bytes {first}-{last}/{size}"}
            return response
        opener = mock.Mock(); opener.open.side_effect = open_range
        def convert(path, *args): return {"markdown": f"pages={len(PdfReader(path).pages)}"}
        with mock.patch("urllib.request.build_opener", return_value=opener), \
             mock.patch.object(converter_app, "process_pdf", side_effect=convert), \
             mock.patch.dict("os.environ", {"CONVERTER_SOURCE_HOSTS": "storage.example.test", "PDF_PAGES_PER_BATCH": "10"}):
            result = converter_app.process_source_batch("https://storage.example.test/large.pdf", size, 10, "gemini", "model", Event())
        self.assertEqual(result["markdown"], "pages=2")
        self.assertTrue(result["done"])
        self.assertLess(sum(fetched), 4 * 1024**2)
