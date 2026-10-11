import importlib.util
import io
import json
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

PIPELINE_ROOT = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_ROOT))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


converter_app = load("converter_app", "app.py")
worker = load("converter_worker", "worker.py")
from app.core.pipeline_control import PipelineRunCancelledError  # noqa: E402
from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal  # noqa: E402


def pdf_bytes(pages):
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    raw = io.BytesIO()
    writer.write(raw)
    return raw.getvalue()


def command(**overrides):
    return {
        "run_id": "convert:doc_1:20", "parent_run_id": "convert:doc_1", "kind": "convert_batch",
        "workspace_id": "ws_1", "user_id": "user_1", "document_id": "doc_1",
        "source_key": "sources/documents/doc_1/original", "start_page": 20, "page_count": 10,
        "provider": "gemini", "model": "model", **overrides,
    }


class FakeStorage:
    def __init__(self, data):
        self.data, self.puts = data, {}

    def stat_object(self, bucket, key):
        return mock.Mock(size=len(self.data))

    def get_object(self, bucket, key, offset=0, length=0):
        response = mock.Mock()
        response.read.return_value = self.data[offset:offset + length]
        return response

    def put_object(self, bucket, key, data, length, content_type):
        self.puts[key] = (data.read(), content_type)


@contextmanager
def storage_with(pages):
    storage = FakeStorage(pdf_bytes(pages))
    with mock.patch.object(worker, "client", return_value=storage), \
         mock.patch.object(worker, "bucket_name", return_value="bucket"), \
         mock.patch.object(converter_app, "missing_commands", return_value=[]):
        yield storage


class ConvertBatchTest(unittest.TestCase):
    def fake_process(self, calls):
        def process(path, provider, model, cancelled, actor):
            from pypdf import PdfReader
            calls.append({"pages": len(PdfReader(path).pages), "actor": actor, "provider": provider, "model": model})
            return {"markdown": f"# {len(PdfReader(path).pages)} pages\n"}
        return process

    def test_range_is_clamped_to_total_pages_and_result_is_written_to_storage(self):
        calls = []
        with storage_with(23) as storage, mock.patch.object(converter_app, "process_pdf", side_effect=self.fake_process(calls)):
            result = worker.convert_batch(command(), threading.Event())
        self.assertEqual(result, {"document_id": "doc_1", "page_start": 21, "page_end": 23, "total_pages": 23,
                                  "result_key": "tmp/convert/ws_1/doc_1/20.md"})
        self.assertEqual(calls[0]["pages"], 3)
        self.assertEqual(storage.puts["tmp/convert/ws_1/doc_1/20.md"],
                         ("# 3 pages\n".encode(), "text/markdown; charset=utf-8"))

    def test_usage_actor_uses_parent_run_id(self):
        calls = []
        with storage_with(5), mock.patch.object(converter_app, "process_pdf", side_effect=self.fake_process(calls)):
            worker.convert_batch(command(start_page=0, page_count=5), threading.Event())
        self.assertEqual(calls[0]["actor"], {"run_id": "convert:doc_1", "workspace_id": "ws_1", "user_id": "user_1"})
        self.assertEqual((calls[0]["provider"], calls[0]["model"]), ("gemini", "model"))

    def test_start_page_at_or_beyond_total_is_rejected(self):
        with storage_with(23), mock.patch.object(converter_app, "process_pdf") as process:
            for start in (23, 40):
                with self.assertRaises(ValueError):
                    worker.convert_batch(command(start_page=start), threading.Event())
        process.assert_not_called()

    def test_source_key_outside_document_source_prefix_is_rejected_before_storage_access(self):
        for key in ("tmp/convert/ws_1/doc_1/0.md", "pipeline-runs/x/pipeline.log", "sources/documents/doc_1/original/../x",
                    "sources/documents/../secret/original", "s3://bucket/sources/documents/doc_1/original",
                    "/sources/documents/doc_1/original", "sources/documents//original", "sources/documents/doc_1/other"):
            with self.subTest(key=key), mock.patch.object(worker, "client") as storage, self.assertRaises(ValueError):
                worker.convert_batch(command(source_key=key), threading.Event())
            storage.assert_not_called()

    def test_invalid_range_and_ids_are_rejected(self):
        for overrides in ({"page_count": 0}, {"page_count": 51}, {"start_page": -1}, {"start_page": "0"},
                          {"page_count": True}, {"document_id": "../x"}, {"workspace_id": "a/b"},
                          {"parent_run_id": ""}, {"kind": "document"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                worker.parse_command(command(**overrides))

    def test_event_envelope_matches_task_worker(self):
        from app.workers import task_worker
        expected = task_worker._event(command(), "succeeded", payload={"a": 1}, with_request=False)
        self.assertEqual(worker._event(command(), "succeeded", payload={"a": 1}), expected)
        self.assertEqual(expected["event_id"], "convert_batch:convert:doc_1:20:succeeded")


class JournalStub:
    """postgres_task_journal.execute가 쓰는 DB 접근만 메모리로 바꾼다. execute 본문은 실제 코드다."""

    def __init__(self):
        self.row = None
        self.status = "running"

    def __enter__(self):
        names = {
            "execution_lock": contextmanager(lambda run_id: (yield)),
            "register": self.register,
            "_begin_attempt": lambda row: None,
            "complete": self.complete,
            "active": lambda run_id: self.status == "running",
            "request_cancel": lambda *args: None,
            "rollback": lambda run_id: None,
            "connect": mock.MagicMock,
        }
        self.patches = [mock.patch.object(journal, name, value) for name, value in names.items()]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *args):
        for patch in self.patches:
            patch.stop()

    def register(self, command):
        if self.row is None:
            self.row = {"status": "running", "result": None, "workspace_id": command["workspace_id"],
                        "user_id": command["user_id"], "id": command["run_id"], "error_code": None}
        return self.row

    def complete(self, run_id, result):
        self.row.update(status="completed", result=result)


class RunCommandTest(unittest.TestCase):
    def test_redelivery_replays_stored_result_without_converting_again(self):
        from app.core.pipeline_control import ReplayedResult
        with JournalStub(), storage_with(23), \
             mock.patch.object(converter_app, "process_pdf", return_value={"markdown": "# a\n"}) as process:
            first = worker.run_command(command())
            second = worker.run_command(command())
        self.assertEqual(process.call_count, 1)
        self.assertNotIsInstance(first, ReplayedResult)
        self.assertIsInstance(second, ReplayedResult)
        self.assertEqual(dict(second), first)

    def test_cancel_request_stops_an_in_flight_batch(self):
        stub = JournalStub()
        started, finished = threading.Event(), threading.Event()

        def process(path, provider, model, cancelled, actor):
            started.set()
            try:
                if not cancelled.wait(5):
                    raise AssertionError("취소 신호가 변환에 전달되지 않았습니다.")
                raise PipelineRunCancelledError("Conversion cancelled")
            finally:
                finished.set()

        with stub, storage_with(23), mock.patch.object(worker, "CANCEL_POLL_SECONDS", 0.02), \
             mock.patch.object(converter_app, "process_pdf", side_effect=process):
            runner = threading.Thread(target=lambda: self.outcome.append(self._run()))
            self.outcome = []
            runner.start()
            self.assertTrue(started.wait(3))
            began = time.monotonic()
            stub.status = "cancel_requested"
            runner.join(3)
        self.assertFalse(runner.is_alive())
        self.assertLess(time.monotonic() - began, 2)
        self.assertTrue(finished.is_set())
        self.assertIsInstance(self.outcome[0], PipelineRunCancelledError)

    def _run(self):
        try:
            return worker.run_command(command())
        except Exception as error:
            return error

    def test_failure_code_is_a_common_code_not_exception_text(self):
        from app.core.failure_codes import failure_code
        for error, code in ((converter_app.InputRejectedError("password secret"), "input_rejected"),
                            (converter_app.ConversionFailedError("Command failed: x"), "internal_error"),
                            (PipelineRunCancelledError("c"), "cancelled")):
            self.assertEqual(failure_code(error), code)
        from app.core.pipeline_control import DeadlineExceededError
        self.assertEqual(failure_code(DeadlineExceededError("d")), "deadline_exceeded")

    def test_poll_interval_exceeds_batch_deadline(self):
        self.assertGreater(worker.MAX_POLL_INTERVAL_MS, worker.BATCH_DEADLINE_SECONDS * 1000)


if __name__ == "__main__":
    unittest.main()
