"""만료된 수정안 artifact의 객체와 행을 함께 지운다.

`expires_at`은 읽기 필터일 뿐이라, 승인되지 않은 수정안의 본문은 여기서 지우지 않으면 계속 남는다.
`expires_at IS NULL`(되돌리기용)은 조건에 걸리지 않아 지워지지 않는다.

행과 run을 잠근 채 객체를 먼저 지우고 행을 지운다. 파기(data_purge)와 같은 순서다. 행을 먼저 지우면 객체 삭제가 실패한 뒤
지울 키를 다시 찾을 수 없다. 객체를 지운 뒤 커밋 전에 죽어도 다음 주기에 없는 객체를 성공으로 보고 행을 지운다.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from app.modules.agent_run.infrastructure.postgres_agent_run_repository import _run_object_prefix
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database
from app.modules.wiki_ingestion.infrastructure.object_storage import (
    bucket_name,
    delete_object,
    is_missing_object_error,
    split_storage_uri,
)


logger = logging.getLogger(__name__)

ARTIFACT_CLEANUP_BATCH_SIZE = 100
# 저장소 장애면 객체마다 재시도까지 기다려 agent job이 함께 밀린다. 이만큼 실패하면 이번 주기를 끝낸다.
MAX_STORAGE_FAILURES_PER_CYCLE = 3
# 계획을 세우거나 실행·되돌리는 중인 run은 건드리지 않는다. 멈춘 run은 lease 정리로 끝난 뒤 지워진다.
_IN_PROGRESS_RUN_STATUSES = ("queued", "planning", "executing", "verifying", "cancel_requested", "rolling_back")


@dataclass(frozen=True)
class ExpiredArtifactCleanup:
    deleted: int = 0
    failed: int = 0  # 객체 삭제에 실패해 행을 남겼다. 다음 주기에 다시 시도한다.
    rejected: int = 0  # 키가 run 범위를 벗어나 객체도 행도 건드리지 않았다.


def delete_expired_artifacts(
    *,
    grace_seconds: float,
    limit: int = ARTIFACT_CLEANUP_BATCH_SIZE,
) -> ExpiredArtifactCleanup:
    with database.connect_ai() as conn:
        # 승인·취소는 run 행을 FOR UPDATE로 잠그므로 진행 중인 승인과 겹치면 건너뛴다. 이쪽이 먼저 잠그면 승인은 커밋을
        # 기다렸다가 수정안이 없음을 보고 거절된다. checkpoint 트리거의 KEY SHARE는 NO KEY UPDATE와 충돌하지 않는다.
        rows = conn.execute(
            """
            SELECT artifact.id, artifact.run_id, artifact.object_key, artifact.preview_object_key
            FROM agent_run_artifacts artifact
            JOIN agent_runs run ON run.id = artifact.run_id
            WHERE artifact.expires_at < now() - %s * interval '1 second'
              AND run.status <> ALL(%s)
            ORDER BY artifact.expires_at, artifact.id
            LIMIT %s
            FOR UPDATE OF artifact SKIP LOCKED
            FOR NO KEY UPDATE OF run SKIP LOCKED
            """,
            (grace_seconds, list(_IN_PROGRESS_RUN_STATUSES), limit),
        ).fetchall()
        removed: list[str] = []
        failed = 0
        rejected = 0
        for row in rows:
            keys = [key for key in (row["object_key"], row["preview_object_key"]) if key]
            if not all(_is_in_run_scope(key, row["run_id"]) for key in keys):
                rejected += 1
                logger.error("run 범위를 벗어난 artifact 키라 정리하지 않습니다: artifact_id=%s", row["id"])
                continue
            try:
                for key in keys:
                    _delete_if_present(key)
            except Exception as exc:
                # 저장소 오류 원문은 키·요청 정보를 담을 수 있어 타입만 남긴다.
                failed += 1
                logger.warning("만료된 artifact 객체 삭제 실패: artifact_id=%s type=%s", row["id"], type(exc).__name__)
                if failed >= MAX_STORAGE_FAILURES_PER_CYCLE:
                    break
                continue
            removed.append(row["id"])
        if removed:
            conn.execute("DELETE FROM agent_run_artifacts WHERE id = ANY(%s)", (removed,))
    return ExpiredArtifactCleanup(deleted=len(removed), failed=failed, rejected=rejected)


def _is_in_run_scope(key: str, run_id: str) -> bool:
    bucket, object_name = split_storage_uri(key)
    return (
        bucket == bucket_name()
        and object_name.startswith(_run_object_prefix(run_id))
        and ".." not in object_name.split("/")
    )


def _delete_if_present(key: str) -> None:
    try:
        delete_object(key)
    except Exception as exc:
        if not is_missing_object_error(exc):
            raise
