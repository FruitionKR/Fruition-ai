import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.pipeline_control import PipelineRunCancelledError
from app.modules.wiki_generation.infrastructure.pipeline_log import PipelineLog


def test_emit_sends_internal_token_to_log_callback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "test-internal-token")
    log = PipelineLog(
        tmp_path / "pipeline.log",
        callback_url="http://backend/pipeline-events",
    )

    with patch("urllib.request.urlopen") as urlopen:
        log.emit("변환", "문서를 변환했습니다.")

    request = urlopen.call_args.args[0]
    assert request.get_header("X-internal-token") == "test-internal-token"


def test_emit_reports_pipeline_progress(tmp_path: Path) -> None:
    calls: list[str] = []
    log = PipelineLog(
        tmp_path / "pipeline.log",
        progress_callback=lambda: calls.append("touch"),
    )

    log.emit("변환", "문서를 변환했습니다.")

    assert calls == ["touch"]


def test_emit_keeps_pipeline_running_when_heartbeat_fails(tmp_path: Path) -> None:
    def fail_heartbeat() -> None:
        raise RuntimeError("database unavailable")

    log_path = tmp_path / "pipeline.log"
    log = PipelineLog(log_path, progress_callback=fail_heartbeat)

    log.emit("변환", "문서를 변환했습니다.")

    content = log_path.read_text(encoding="utf-8")
    assert "문서를 변환했습니다." in content
    assert "heartbeat 갱신 실패" in content
    assert "database unavailable" in content


def test_emit_propagates_inactive_pipeline_signal(tmp_path: Path) -> None:
    log = PipelineLog(
        tmp_path / "pipeline.log",
        progress_callback=lambda: False,
    )

    with pytest.raises(PipelineRunCancelledError):
        log.emit("변환", "문서를 변환했습니다.")


def test_emit_excludes_private_llm_fields_from_callback_payload(tmp_path: Path) -> None:
    log = PipelineLog(tmp_path / "pipeline.log", callback_url="http://backend/events")
    secret_values = {
        "top-password",
        "https://top-base.example",
        "top-api-key",
        "https://top-endpoint.example",
        "top-access-token",
        "nested-password",
        "https://nested-base.example",
        "nested-api-key",
        "https://nested-endpoint.example",
        "nested-secret",
    }

    with patch("urllib.request.urlopen") as urlopen:
        log.emit(
            "API 설정",
            "클라이언트를 준비했습니다.",
            {
                "provider": "gemini",
                "model": "gemini-3.1-flash-lite",
                "count": 3,
                "password": "top-password",
                "baseURL": "https://top-base.example",
                "apiKey": "top-api-key",
                "endpoint": "https://top-endpoint.example",
                "accessToken": "top-access-token",
                "metadata": {
                    "dbPassword": "nested-password",
                    "apiBaseURL": "https://nested-base.example",
                    "nestedApiKey": "nested-api-key",
                    "apiEndpoint": "https://nested-endpoint.example",
                    "clientSecret": "nested-secret",
                    "ordinary": {"value": "keep"},
                },
            },
        )

    event = json.loads(urlopen.call_args.args[0].data)
    log_content = (tmp_path / "pipeline.log").read_text(encoding="utf-8")
    serialized_event = json.dumps(event, ensure_ascii=False)

    for secret in secret_values:
        assert secret not in serialized_event
        assert secret not in log_content

    assert event["data"] == {
        "provider": "gemini",
        "model": "gemini-3.1-flash-lite",
        "count": "3",
        "metadata": "{'ordinary': {'value': 'keep'}}",
    }
    assert "provider: gemini" in log_content
    assert "count: 3" in log_content


def test_durable_parallel_emit_and_retry_replaces_previous_attempt(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from app.modules.wiki_generation.infrastructure import pipeline_log
    snapshots = []
    monkeypatch.setattr(pipeline_log, "write_pipeline_log", lambda uri, text: snapshots.append(text))
    log = PipelineLog("s3://test/pipeline-runs/run-1/pipeline.log", run_id="run-1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: log.emit("진행", f"line-{i:03d}"), range(100)))
    assert len(snapshots) == 100
    for i in range(100):
        assert snapshots[-1].count(f"line-{i:03d}") == 1
    retry = PipelineLog(log.path, run_id="run-1")
    retry.emit("재시도", "새 시도")
    assert "새 시도" in snapshots[-1]
    assert "line-" not in snapshots[-1]


def test_durable_log_bypasses_cancellation_journal_and_preserves_callback_errors(monkeypatch):
    from unittest.mock import Mock
    from urllib.error import URLError
    from app.core.pipeline_control import task_run_id
    from app.modules.wiki_ingestion.infrastructure import object_storage
    client = Mock()
    monkeypatch.setattr(object_storage, "client", lambda: client)
    def heartbeat():
        raise RuntimeError("heartbeat unavailable")
    token = task_run_id.set("cancelled-run")
    try:
        with patch("app.modules.task_cancellation.infrastructure.object_change_journal.change_object") as journal, patch("urllib.request.urlopen", side_effect=URLError("callback unavailable")):
            log = PipelineLog("s3://test/pipeline-runs/cancelled-run/pipeline.log", progress_callback=heartbeat, callback_url="http://invalid.test/callback")
            log.emit("진행", "보존")
            content = client.put_object.call_args.args[2].getvalue().decode()
            assert "보존" in content
            assert "heartbeat 갱신 실패" in content
            assert "로그 전송 실패" in content
            log.progress_callback = lambda: False
            with pytest.raises(PipelineRunCancelledError):
                log.emit("취소", "취소 시점까지 보존")
            assert "취소 시점까지 보존" in client.put_object.call_args.args[2].getvalue().decode()
            journal.assert_not_called()
    finally:
        task_run_id.reset(token)


def test_run_pipeline_uses_durable_log_before_failure_without_debug_files(tmp_path, monkeypatch):
    import run_lab
    from app.modules.wiki_ingestion.application.models import PipelineRunCommand
    from app.modules.wiki_generation.infrastructure import pipeline_log
    from app.modules.wiki_ingestion.infrastructure.object_storage import pipeline_log_uri
    writes = []
    monkeypatch.setattr(pipeline_log, "write_pipeline_log", lambda uri, text: writes.append((uri, text)))
    command = PipelineRunCommand(run_id="run-1", input="input.md", input_name="input.md", out=str(tmp_path / "scratch"), user_id="user-1", workspace_id="ws-1", input_markdown="# test", mode="offline", provider="openai", model="gpt-5-nano", save_debug_json=False)
    with patch.object(run_lab, "_load_pipeline_prompts", side_effect=RuntimeError("stage failed")):
        with pytest.raises(RuntimeError, match="stage failed"):
            run_lab.run_pipeline(command)
    assert writes[0][0] == pipeline_log_uri("run-1")
    assert "파이프라인 실행을 시작했습니다" in writes[0][1]
    assert not list((tmp_path / "scratch").iterdir())


@pytest.mark.parametrize("code", ["NoSuchKey", "AccessDenied", "NoSuchBucket"])
def test_log_reader_only_allows_not_yet_started_missing_key(code):
    from minio.error import S3Error
    from app.modules.wiki_ingestion.infrastructure import pipeline_run_adapters
    error = S3Error(response=None, code=code, message="storage failure", resource="resource", request_id="request", host_id="host")
    with patch.object(pipeline_run_adapters, "read_text_object", side_effect=error):
        reader = pipeline_run_adapters.ObjectStoragePipelineLogReader()
        if code == "NoSuchKey":
            assert reader.read_text("s3://test/pipeline-runs/run-1/pipeline.log") == ""
        else:
            with pytest.raises(S3Error):
                reader.read_text("s3://test/pipeline-runs/run-1/pipeline.log")
