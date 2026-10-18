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
                "nvidia_nccl_cu13": "2.30.7",
                "cuda-toolkit": "13.0.3",
                "triton": "3.8.0",
            }
        )

        self.assertEqual(
            errors,
            [
                "torch==2.14.0 is not a +cpu wheel",
                "GPU packages present: cuda-toolkit, nvidia-cudnn-cu13, nvidia_nccl_cu13, triton",
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
            env=None,
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
                ) as restoration, self.assertLogs("converter", "WARNING") as logs:
                    result = process_pdf(
                        b"pdf", usage_actor={"run_id": "r", "workspace_id": "w", "user_id": None}
                    )

        command = restoration.call_args.args[0]
        self.assertEqual(command[command.index("--mode") + 1], "crop-first")
        self.assertEqual(
            command[command.index("--selective-provider") + 1], "gemini"
        )
        self.assertEqual(
            command[command.index("--selective-model") + 1],
            "gemini-3.5-flash-lite",
        )
        self.assertEqual(result["repair_summary"]["provider"], "gemini")
        # user_id가 빠진 요청은 converter 운영 로그에 경고로 남는다.
        self.assertIn("사용자 귀속 없는 변환 요청", logs.output[0])
        # selective repair가 상속받을 사용량 원장 actor
        env = restoration.call_args.args[5]
        self.assertEqual(
            json.loads(env["MODEL_USAGE_SCOPE"]),
            {"run_id": "r", "workspace_id": "w", "user_id": None, "kind": "document_conversion"},
        )
        self.assertFalse(asset_paths[0].exists())
        marker = "data:image/png;base64,"
        self.assertIn(marker, result["markdown"])
        encoded = result["markdown"].split(marker, 1)[1].split(")", 1)[0]
        self.assertEqual(base64.b64decode(encoded), fixture)


class ConverterCancellationTest(unittest.TestCase):
    def test_cancellation_terminates_running_process(self):
        import tempfile
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from app.core.pipeline_control import PipelineRunCancelledError
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
                with self.assertRaises(PipelineRunCancelledError):
                    future.result(timeout=3)
                with self.assertRaises(ProcessLookupError):
                    os.kill(process_id, 0)


class ConverterRequestDeadlineTest(unittest.TestCase):
    def test_stage_timeouts_are_capped_to_remaining_time(self):
        from app.core.pipeline_control import request_deadline
        self.assertEqual(converter_app._within_deadline(60), 60)
        with request_deadline(30):
            self.assertLessEqual(converter_app._within_deadline(900), 30)
            self.assertEqual(converter_app._within_deadline(5), 5)

    def test_expired_deadline_stops_before_running_any_command(self):
        from app.core.pipeline_control import DeadlineExceededError, request_deadline
        with mock.patch.object(converter_app, "missing_commands", return_value=[]), \
             mock.patch.object(converter_app, "run_to_file") as diagnostic, \
             mock.patch.object(converter_app, "run") as restoration, \
             request_deadline(-1), self.assertRaises(DeadlineExceededError):
            converter_app.process_pdf(b"pdf")
        diagnostic.assert_not_called()
        restoration.assert_not_called()


def pdf_bytes(pages):
    import io
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    raw = io.BytesIO()
    writer.write(raw)
    return raw.getvalue()


class FakeStorage:
    """minio.get_object(offset, length)를 흉내 낸다. 요청한 범위를 기록한다."""

    def __init__(self, data):
        self.data, self.requests = data, []

    def get_object(self, bucket, key, offset=0, length=0):
        import io
        self.requests.append((offset, length))
        response = mock.Mock()
        response.read.return_value = self.data[offset:offset + length]
        return response


class StorageRangeReaderTest(unittest.TestCase):
    def test_reads_fetch_one_block_and_reuse_the_cache(self):
        from threading import Event
        storage = FakeStorage(b"x" * 3000000)
        reader = converter_app.StorageRangeReader(storage, "b", "k", 3000000, Event())
        reader.seek(-12, 2)
        self.assertEqual(reader.read(12), b"x" * 12)
        reader.seek(-6, 2)
        self.assertEqual(reader.read(6), b"x" * 6)
        self.assertEqual(len(storage.requests), 1)
        self.assertEqual(storage.requests[0][1], 3000000 - 2 * 1024**2)

    def test_incomplete_range_fails_the_attempt(self):
        from threading import Event
        storage = FakeStorage(b"x" * 10)
        reader = converter_app.StorageRangeReader(storage, "b", "k", 100, Event())
        with self.assertRaises(converter_app.ConversionFailedError):
            reader.read(1)

    def test_cancelled_event_stops_reading(self):
        from threading import Event
        from app.core.pipeline_control import PipelineRunCancelledError
        cancelled = Event()
        cancelled.set()
        reader = converter_app.StorageRangeReader(FakeStorage(b"x" * 10), "b", "k", 10, cancelled)
        with self.assertRaises(PipelineRunCancelledError):
            reader.read(1)

    def test_valid_three_gib_pdf_is_parsed_using_only_remote_ranges(self):
        import re
        from threading import Event
        from pypdf import PdfReader
        raw = pdf_bytes(12)
        split = raw.index(b"\nxref\n") + 1
        prefix = raw[:split]
        xref_offset = 3 * 1024**3
        suffix = re.sub(rb"startxref\n[0-9]+", f"startxref\n{xref_offset}".encode(), raw[split:])
        size = xref_offset + len(suffix)
        fetched = []

        class SparseStorage:
            def get_object(self, bucket, key, offset=0, length=0):
                data = bytearray(b" " * length)
                for start, value in [(0, prefix), (xref_offset, suffix)]:
                    lo, hi = max(start, offset), min(start + len(value), offset + length)
                    if lo < hi:
                        data[lo - offset:hi - offset] = value[lo - start:hi - start]
                fetched.append(len(data))
                response = mock.Mock()
                response.read.return_value = bytes(data)
                return response

        stream = converter_app.StorageRangeReader(SparseStorage(), "b", "k", size, Event())
        reader, total = converter_app.open_pdf(stream)
        self.assertEqual(total, 12)
        self.assertLess(sum(fetched), 4 * 1024**2)

    def test_encrypted_and_invalid_pdfs_are_rejected(self):
        import io
        from threading import Event
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.encrypt("secret")
        raw = io.BytesIO()
        writer.write(raw)
        for data in (raw.getvalue(), b"not a pdf"):
            stream = converter_app.StorageRangeReader(FakeStorage(data), "b", "k", len(data), Event())
            with self.assertRaises(converter_app.InputRejectedError):
                converter_app.open_pdf(stream)


class WorkspaceRepairModelTest(unittest.TestCase):
    """변환 repair는 워크스페이스 공용 모델 설정을 쓰므로 Claude·제거된 모델을 기본 모델로 대체한다."""

    def test_claude_or_removed_model_falls_back_to_workspace_default(self) -> None:
        real = converter_app.resolve_workspace_llm_selection
        for provider, model in (("claude", "claude-haiku-4-5-20251001"), ("openai", "gpt-5.5")):
            with self.subTest(provider=provider, model=model):
                resolved = []
                with mock.patch.object(
                    converter_app, "resolve_workspace_llm_selection",
                    side_effect=lambda p, m: resolved.append(real(p, m)) or resolved[-1],
                ), mock.patch.object(converter_app, "missing_commands", return_value=["pdftotext"]):
                    with self.assertRaisesRegex(RuntimeError, "Missing converter commands"):
                        process_pdf(b"pdf", provider, model)
                self.assertEqual(resolved, [("gemini", "gemini-3.5-flash-lite")])


class SplitOnFailureTest(unittest.TestCase):
    """실패·시간 초과한 범위는 절반으로 나눠 1쪽까지 재시도한다."""

    def setUp(self):
        from pypdf import PdfReader
        import io
        self.reader = PdfReader(io.BytesIO(pdf_bytes(10)))

    def run_pages(self, behavior, start=0, end=10, cancelled=None):
        from threading import Event
        calls = []

        def process(path, provider, model, event, actor):
            from pypdf import PdfReader
            count = len(PdfReader(path).pages)
            calls.append(count)
            outcome = behavior(len(calls), count)
            if isinstance(outcome, Exception):
                raise outcome
            return {"markdown": outcome}

        with mock.patch.object(converter_app, "process_pdf", side_effect=process):
            try:
                return converter_app.convert_pages(self.reader, start, end, "gemini", "m", cancelled or Event(), {}), calls
            except Exception as error:
                error.calls = calls
                raise

    def test_success_does_not_split(self):
        markdown, calls = self.run_pages(lambda n, count: "all")
        self.assertEqual((markdown, calls), ("all", [10]))

    def test_failure_splits_in_half_and_keeps_page_order(self):
        markdown, calls = self.run_pages(
            lambda n, count: converter_app.ConversionFailedError("x") if n == 1 else f"[{count}]")
        self.assertEqual(calls, [10, 5, 5])
        self.assertEqual(markdown, "[5]\n\n[5]")

    def test_timeout_splits_but_one_page_failure_fails_the_batch(self):
        from app.core.pipeline_control import DeadlineExceededError
        with self.assertRaises(DeadlineExceededError) as raised:
            self.run_pages(lambda n, count: DeadlineExceededError("attempt"))
        # 10 -> 5 -> 2 -> 1 쪽까지 내려가 첫 1쪽 실패로 끝난다.
        self.assertEqual(raised.exception.calls, [10, 5, 2, 1])

    def test_only_the_failing_half_is_split_again(self):
        def behavior(n, count):
            if n == 1 or n == 3:
                return converter_app.ConversionFailedError("x")
            return f"[{count}]"
        markdown, calls = self.run_pages(behavior)
        self.assertEqual(calls, [10, 5, 5, 2, 3])
        self.assertEqual(markdown, "[5]\n\n[2]\n\n[3]")

    def test_input_rejected_and_cancelled_are_not_split(self):
        from threading import Event
        from app.core.pipeline_control import PipelineRunCancelledError
        for error in (converter_app.InputRejectedError("encrypted"), PipelineRunCancelledError("cancelled")):
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)) as raised:
                self.run_pages(lambda n, count, error=error: error)
            self.assertEqual(raised.exception.calls, [10])
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(PipelineRunCancelledError) as raised:
            self.run_pages(lambda n, count: "ok", cancelled=cancelled)
        self.assertEqual(raised.exception.calls, [])

    def test_expired_batch_deadline_is_not_split(self):
        from app.core.pipeline_control import DeadlineExceededError, request_deadline
        with request_deadline(-1), self.assertRaises(DeadlineExceededError) as raised:
            self.run_pages(lambda n, count: converter_app.ConversionFailedError("x"))
        self.assertEqual(raised.exception.calls, [10])

    def test_each_attempt_gets_its_own_timeout(self):
        from app.core.pipeline_control import remaining_seconds
        seen = []
        with mock.patch.object(converter_app, "ATTEMPT_TIMEOUT_SECONDS", 1500):
            self.run_pages(lambda n, count: seen.append(remaining_seconds()) or "ok")
        self.assertLessEqual(seen[0], 1500)
        self.assertGreater(seen[0], 1400)


if __name__ == "__main__":
    unittest.main()
