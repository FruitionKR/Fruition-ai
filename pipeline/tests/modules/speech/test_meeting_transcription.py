import asyncio
import wave
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import numpy as np
import pytest

from app.core.pipeline_control import PipelineRunCancelledError, task_cancellation_scope
from app.modules.model_usage.infrastructure import usage_ledger
from app.modules.speech.application.transcribe_meeting import TranscribeMeetingRecording
from app.modules.speech.domain.errors import MeetingTranscriptionError, SpeechUnavailableError
from app.modules.speech.infrastructure import audio_chunks, meeting_storage, openai_speech
from app.modules.speech.infrastructure.meeting_storage import S3MeetingStorage, checkpoint_prefix
from app.workers import task_worker

KEY = "meetings/m1/recording-" + "a" * 36 + ".mp3"
COMMAND = {
    "run_id": "meeting_transcription:m1:1", "kind": "meeting_transcription", "workspace_id": "ws",
    "user_id": "user", "meeting_id": "m1", "recording_key": KEY, "content_type": "audio/mpeg",
}


def _write_wav(path: Path, samples: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.astype("<i2").tobytes())
    return path


def _tone(seconds: float, gap: tuple[float, float] | None = None) -> np.ndarray:
    t = np.arange(int(seconds * 16000)) / 16000
    samples = (8000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)
    if gap:
        samples[int(gap[0] * 16000):int(gap[1] * 16000)] = 0
    return samples


@pytest.fixture
def short_chunks(monkeypatch):
    """목표 5초·탐색 1초로 줄여 실제 PyAV 경로를 작은 파일로 검증한다."""
    monkeypatch.setattr(audio_chunks, "TARGET_SAMPLES", 5 * 16000)
    monkeypatch.setattr(audio_chunks, "SEARCH_SAMPLES", 16000)


def test_find_cut_picks_lowest_energy_window_and_never_exceeds_target():
    rng = np.random.default_rng(0)
    samples = rng.integers(-3000, 3000, audio_chunks.TARGET_SAMPLES + 1000).astype(np.int16)
    quiet = audio_chunks.TARGET_SAMPLES - 10 * 16000
    samples[quiet:quiet + 1600] = 0
    cut = audio_chunks.find_cut(samples)
    assert cut == quiet + 800 and cut <= audio_chunks.TARGET_SAMPLES
    flat = np.zeros(audio_chunks.TARGET_SAMPLES + 1000, dtype=np.int16)
    assert audio_chunks.find_cut(flat) <= audio_chunks.TARGET_SAMPLES


def test_split_recording_cuts_at_silence_without_loss_or_overlap(tmp_path, short_chunks):
    samples = _tone(12, gap=(4.3, 4.45))
    source = _write_wav(tmp_path / "in.wav", samples)
    paths, duration = audio_chunks.split_recording(source, tmp_path / "out")
    assert duration == pytest.approx(12.0, abs=0.05) and len(paths) == 3
    lengths = []
    for path in paths:
        with wave.open(str(path)) as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
            lengths.append(wav.getnframes())
    assert lengths[0] <= 5 * 16000 and abs(lengths[0] - int(4.375 * 16000)) <= 1600  # 무음 창 안에서 자른다
    assert sum(lengths) == pytest.approx(len(samples), abs=64)  # 겹침·손실 없음


def test_split_recording_rejects_over_limit_and_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_chunks, "MAX_SAMPLES", 2 * 16000)
    long = _write_wav(tmp_path / "long.wav", _tone(3))
    with pytest.raises(MeetingTranscriptionError) as too_long:
        audio_chunks.split_recording(long, tmp_path / "o1")
    assert too_long.value.code == "MEETING_RECORDING_TOO_LONG"
    garbage = tmp_path / "bad.mp3"
    garbage.write_bytes(b"not audio" * 100)
    with pytest.raises(MeetingTranscriptionError) as bad:
        audio_chunks.split_recording(garbage, tmp_path / "o2")
    assert bad.value.code == "MEETING_RECORDING_UNREADABLE"


class FakeStorage:
    def __init__(self, done=None):
        self.done = dict(done or {})
        self.saved = {}
        self.downloaded = 0

    def download(self, key, path):
        self.downloaded += 1
        path.write_bytes(b"x")

    def load_checkpoints(self, workspace_id, run_id):
        return dict(self.done)

    def save_checkpoint(self, workspace_id, run_id, index, text):
        self.saved[index] = text


class FakeSpeech:
    def __init__(self, texts, delays=None):
        self.texts, self.delays, self.calls = texts, delays or {}, []

    async def transcribe(self, audio, media_type, deadline_seconds=None):
        index = int(audio.decode())
        self.calls.append(index)
        await asyncio.sleep(self.delays.get(index, 0))
        text = self.texts[index]
        if isinstance(text, Exception):
            raise text
        return text


def _use_case(speech, storage, count, concurrency=4, deadline=60):
    def split(recording, out_dir):
        out_dir.mkdir()
        paths = []
        for i in range(count):
            path = out_dir / f"{i}.wav"
            path.write_bytes(str(i).encode())
            paths.append(path)
        return paths, 1234.56

    return TranscribeMeetingRecording(speech, storage, split, concurrency, deadline)


def _run(use_case):
    return asyncio.run(use_case.execute("ws", "run", "m1", KEY))


def test_chunks_join_in_order_even_when_completed_out_of_order():
    speech = FakeSpeech(["첫째", "둘째", "", "넷째"], delays={0: 0.05})
    result = _run(_use_case(speech, FakeStorage(), 4))
    assert result == {"meeting_id": "m1", "text": "첫째 둘째 넷째", "duration_seconds": 1234.6, "chunks": 4}


def test_redelivery_calls_provider_only_for_missing_chunks():
    speech = FakeSpeech(["a", "b", "c", "d"])
    storage = FakeStorage(done={0: "a", 2: "c"})
    result = _run(_use_case(speech, storage, 4))
    assert sorted(speech.calls) == [1, 3] and result["text"] == "a b c d"
    assert set(storage.saved) == {1, 3}


def test_empty_and_too_long_transcripts_fail_with_codes():
    with pytest.raises(MeetingTranscriptionError) as empty:
        _run(_use_case(FakeSpeech(["", ""]), FakeStorage(), 2))
    assert empty.value.code == "MEETING_TRANSCRIPT_EMPTY"
    with pytest.raises(MeetingTranscriptionError) as long:
        _run(_use_case(FakeSpeech(["가" * 60000, "나" * 40001]), FakeStorage(), 2))
    assert long.value.code == "MEETING_TRANSCRIPT_TOO_LONG"


def test_over_limit_recording_is_rejected_before_any_provider_call(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_chunks, "MAX_SAMPLES", 2 * 16000)
    speech = FakeSpeech(["x"])
    storage = FakeStorage()
    storage.download = lambda key, path: _write_wav(path, _tone(3))
    use_case = TranscribeMeetingRecording(speech, storage, audio_chunks.split_recording, 4, 60)
    with pytest.raises(MeetingTranscriptionError) as error:
        _run(use_case)
    assert error.value.code == "MEETING_RECORDING_TOO_LONG" and speech.calls == []


def test_unreadable_recording_fails_before_any_provider_call():
    speech = FakeSpeech(["x"])
    storage = FakeStorage()
    storage.download = lambda key, path: path.write_bytes(b"garbage" * 50)
    use_case = TranscribeMeetingRecording(speech, storage, audio_chunks.split_recording, 4, 60)
    with pytest.raises(MeetingTranscriptionError) as error:
        _run(use_case)
    assert error.value.code == "MEETING_RECORDING_UNREADABLE" and speech.calls == []


def test_cancel_stops_remaining_chunks():
    speech = FakeSpeech(["a", "b", "c", "d"])
    checks = iter([True, True, False])  # 다운로드 뒤·첫 구간 호출 전은 활성, 둘째 구간 호출 전에 취소

    def active():
        return next(checks, False)

    with task_cancellation_scope(active), pytest.raises(PipelineRunCancelledError):
        _run(_use_case(speech, FakeStorage(), 4, concurrency=1))
    assert speech.calls == [0]


def test_provider_failure_keeps_code_and_stops_other_chunks():
    speech = FakeSpeech(["a", SpeechUnavailableError("x", "provider_rate_limited"), "c", "d"])
    with pytest.raises(SpeechUnavailableError) as error:
        _run(_use_case(speech, FakeStorage(), 4, concurrency=1))
    assert error.value.code == "provider_rate_limited" and speech.calls == [0, 1]


def test_deadline_exceeded_is_reported_as_deadline_code():
    from app.core.failure_codes import failure_code

    speech = FakeSpeech(["a"], delays={0: 5})
    with pytest.raises(PipelineRunCancelledError) as error:
        _run(_use_case(speech, FakeStorage(), 1, deadline=0.2))
    assert failure_code(error.value) == "deadline_exceeded"


def test_each_chunk_call_is_recorded_in_ledger_with_run_id_and_kind(monkeypatch):
    inserts = []

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda sql, params=(): inserts.append(params) if sql.lstrip().startswith("INSERT") else None)

    monkeypatch.setattr(usage_ledger.database, "connect_ai", connect)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        openai_speech.httpx, "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"text": "전사"}))),
    )
    speech = openai_speech.OpenAISpeech("fake-key")
    with usage_ledger.usage_scope(COMMAND):
        result = _run(_use_case(speech, FakeStorage(), 3))
    assert result["chunks"] == 3 and len(inserts) == 3
    assert all(p[1:5] == (COMMAND["run_id"], "ws", "user", "meeting_transcription") for p in inserts)


def test_transcribe_maps_provider_status_to_codes(monkeypatch):
    original = httpx.AsyncClient

    def client_for(status):
        return lambda **kw: original(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={})))

    for status, code in ((429, "provider_rate_limited"), (500, "provider_unavailable")):
        monkeypatch.setattr(openai_speech.httpx, "AsyncClient", client_for(status))
        with pytest.raises(SpeechUnavailableError) as error:
            asyncio.run(openai_speech.OpenAISpeech("k").transcribe(b"a", "audio/wav"))
        assert error.value.code == code


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, bucket, key, data, length, content_type):
        self.objects[key] = data.read()

    def list_objects(self, bucket, prefix, recursive):
        return [SimpleNamespace(object_name=k) for k in sorted(self.objects) if k.startswith(prefix)]

    def get_object(self, bucket, key):
        return SimpleNamespace(read=lambda: self.objects[key], close=lambda: None, release_conn=lambda: None)

    def remove_object(self, bucket, key):
        self.objects.pop(key, None)


def test_checkpoints_roundtrip_under_ai_prefix_and_clear(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(meeting_storage.object_storage, "client", lambda: fake)
    store = S3MeetingStorage()
    store.save_checkpoint("ws", "run", 0, "가")
    store.save_checkpoint("ws", "run", 1, "")
    store.save_checkpoint("ws", "other-run", 0, "남음")
    assert all(k.startswith("pipeline-runs/meeting-transcription/ws/") for k in fake.objects)
    assert store.load_checkpoints("ws", "run") == {0: "가", 1: ""}
    store.clear_checkpoints("ws", "run")
    assert store.load_checkpoints("ws", "run") == {} and store.load_checkpoints("ws", "other-run") == {0: "남음"}
    assert checkpoint_prefix("ws", "run") != checkpoint_prefix("ws", "other-run")


@pytest.mark.parametrize("outcome", ["ok", "fail", "executing"])
def test_checkpoints_are_deleted_on_terminal_state_but_not_while_another_worker_runs(outcome):
    from app.modules.task_cancellation.infrastructure.postgres_task_journal import TaskAlreadyExecutingError

    effects = {"ok": {"return_value": {}}, "fail": {"side_effect": RuntimeError("x")},
               "executing": {"side_effect": TaskAlreadyExecutingError("x")}}
    with (
        patch.object(task_worker, "_execute_controlled", **effects[outcome]),
        patch.object(task_worker.S3MeetingStorage, "clear_checkpoints") as clear,
    ):
        with pytest.raises(Exception) if outcome != "ok" else contextmanager(lambda: (yield))():
            task_worker._handle_controlled(COMMAND)
    assert clear.call_count == (0 if outcome == "executing" else 1)


@pytest.mark.parametrize("overrides", [
    {"recording_key": "meetings/other/recording-" + "a" * 36 + ".mp3"},
    {"recording_key": "meetings/m1/recording-x.mp3"},
    {"recording_key": "meetings/m1/recording-" + "a" * 36 + ".ogg"},
    {"content_type": "video/mp4"},
])
def test_command_rejects_bad_key_or_content_type(overrides):
    with pytest.raises(MeetingTranscriptionError) as error:
        task_worker._handle({**COMMAND, **overrides})
    assert error.value.code == "input_rejected"


def test_handler_runs_use_case_and_event_carries_result():
    use_case = MagicMock()
    use_case.execute = MagicMock(return_value=asyncio.sleep(0, {"meeting_id": "m1", "text": "t"}))
    with patch.object(task_worker, "build_meeting_transcription", return_value=use_case):
        payload = task_worker._handle(COMMAND)
    use_case.execute.assert_called_once_with("ws", COMMAND["run_id"], "m1", KEY)
    event = task_worker._event(COMMAND, "succeeded", payload=payload)
    assert event["event_id"] == f"meeting_transcription:{COMMAND['run_id']}:succeeded" and event["payload"]["text"] == "t"
