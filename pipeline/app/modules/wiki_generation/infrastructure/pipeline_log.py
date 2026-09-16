from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from app.core.pipeline_control import PipelineRunCancelledError
from app.modules.wiki_ingestion.infrastructure.file_io import append_text
from app.modules.wiki_ingestion.infrastructure.object_storage import write_pipeline_log
from app.workers.event_request import without_top_level_secrets


class PipelineLog:
    def __init__(
        self,
        path: str | Path,
        callback_url: str | None = None,
        run_id: str | None = None,
        progress_callback: Callable[[], bool | None] | None = None,
    ) -> None:
        self.path = str(path) if str(path).startswith("s3://") else Path(path)
        self._lock = Lock()
        self._text = ""
        self.callback_url = callback_url
        self.run_id = run_id
        self.progress_callback = progress_callback
        if isinstance(self.path, Path) and self.path.exists():
            self.path.unlink()

    def _append(self, text: str) -> None:
        # 한 실행의 병렬 source/concept emit과 S3 overwrite를 함께 직렬화한다.
        # 재시도는 새 PipelineLog를 만들어 이전 시도의 로그를 대체한다.
        with self._lock:
            if isinstance(self.path, Path):
                append_text(self.path, text)
            else:
                self._text += text
                write_pipeline_log(self.path, self._text)

    def emit(self, stage: str, message: str, data: dict[str, Any] | None = None) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        safe_data = without_top_level_secrets(data or {})
        event = {
            "run_id": self.run_id,
            "timestamp": now,
            "stage": stage,
            "message": message,
            "data": {
                key: str(value)
                for key, value in safe_data.items()
            },
        }
        lines = [f"[{now}] [{stage}] {message}"]
        for key, value in event["data"].items():
            lines.append(f"  - {key}: {value}")
        self._append("\n".join(lines) + "\n")
        if self.progress_callback:
            self._report_progress(event["timestamp"])
        if self.callback_url:
            self._post_event(event)

    def _report_progress(self, timestamp: str) -> None:
        try:
            should_continue = self.progress_callback()
            if should_continue is False:
                raise PipelineRunCancelledError(
                    "Pipeline run cancelled because its document or workspace is inactive."
                )
        except PipelineRunCancelledError:
            raise
        except Exception as exc:
            self._append(f"[{timestamp}] [heartbeat 갱신 실패] {exc}\n")

    def _post_event(self, event: dict[str, Any]) -> None:
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        internal_token = os.environ.get("INTERNAL_CALLBACK_TOKEN", "").strip()
        if internal_token:
            headers["X-Internal-Token"] = internal_token
        request = urllib.request.Request(
            self.callback_url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5):
                pass
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._append(f"[{event['timestamp']}] [로그 전송 실패] {exc}\n")
