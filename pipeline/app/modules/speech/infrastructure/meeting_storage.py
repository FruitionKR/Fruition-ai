"""회의 녹음 읽기와 구간 전사 체크포인트. 체크포인트는 개인정보(전사)라 작업이 끝나면 지운다."""

import hashlib
import logging
from io import BytesIO
from pathlib import Path

from minio.error import S3Error

from app.modules.speech.domain.errors import MeetingTranscriptionError
from app.modules.wiki_ingestion.infrastructure import object_storage

# ai가 쓰고 지우는 접두사다. 워크스페이스 파기가 이 아래를 지운다(data_purge).
CHECKPOINT_ROOT = "pipeline-runs/meeting-transcription"
logger = logging.getLogger(__name__)


def checkpoint_prefix(workspace_id: str, run_id: str) -> str:
    # run_id는 임의 문자를 담을 수 있어 해시로 키 구조를 고정한다.
    return f"{CHECKPOINT_ROOT}/{workspace_id}/{hashlib.sha256(run_id.encode()).hexdigest()[:32]}/"


class S3MeetingStorage:
    def download(self, key: str, path: Path) -> None:
        try:
            object_storage.client().fget_object(object_storage.bucket_name(), key, str(path))
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchBucket"}:
                raise MeetingTranscriptionError("MEETING_RECORDING_UNREADABLE") from exc
            raise

    def load_checkpoints(self, workspace_id: str, run_id: str) -> dict[int, str]:
        client, bucket = object_storage.client(), object_storage.bucket_name()
        prefix = checkpoint_prefix(workspace_id, run_id)
        done: dict[int, str] = {}
        for item in client.list_objects(bucket, prefix=prefix, recursive=True):
            response = client.get_object(bucket, item.object_name)
            try:
                done[int(Path(item.object_name).stem)] = response.read().decode("utf-8")
            finally:
                response.close()
                response.release_conn()
        return done

    def save_checkpoint(self, workspace_id: str, run_id: str, index: int, text: str) -> None:
        data = text.encode("utf-8")
        # write_text_object는 작업 변경 기록(롤백 대상)에 얹히므로 쓰지 않는다. 체크포인트는 롤백해도 남아야 재전달이 건너뛴다.
        object_storage.client().put_object(
            object_storage.bucket_name(), f"{checkpoint_prefix(workspace_id, run_id)}{index:05d}.txt",
            BytesIO(data), length=len(data), content_type="text/plain; charset=utf-8",
        )

    def clear_checkpoints(self, workspace_id: str, run_id: str) -> None:
        """실패해도 작업 결과를 바꾸지 않는다. 남은 객체는 스토리지 lifecycle이 만료시킨다."""
        try:
            client, bucket = object_storage.client(), object_storage.bucket_name()
            for item in client.list_objects(bucket, prefix=checkpoint_prefix(workspace_id, run_id), recursive=True):
                client.remove_object(bucket, item.object_name)
        except Exception as exc:  # noqa: BLE001 - 정리 실패가 끝난 작업을 실패로 바꾸지 않게 한다.
            logger.warning("회의 전사 체크포인트 삭제 실패: run_id=%s error_type=%s", run_id, type(exc).__name__)
