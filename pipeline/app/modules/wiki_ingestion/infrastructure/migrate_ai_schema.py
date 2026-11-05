"""runtime과 분리한 일회성 ai_db migration 명령."""

import logging
import os
from pathlib import Path

import psycopg
from psycopg import sql

logger = logging.getLogger(__name__)
_CONVERTER_UPDATE_COLUMNS = (
    "status", "model", "input_tokens", "output_tokens", "cached_input_tokens", "cache_creation_tokens",
    "reasoning_tokens", "audio_seconds", "input_characters", "duration_ms", "finished_at",
)
# 스키마 트랜잭션 밖에서 CONCURRENTLY로 만드는 인덱스. 없어도 영구 삭제는 표를 훑어 그대로 동작한다.
ONLINE_INDEXES = {
    "idx_ai_task_runs_workspace_status": "ai_task_runs(workspace_id, status)",
    "idx_ai_task_runs_command_document": (
        "ai_task_runs((command->>'document_id')) WHERE command->>'document_id' IS NOT NULL"
    ),
}
# 스키마 적용과 인덱스 빌드가 함께 쥐는 advisory lock. 빌드 중에 스키마를 적용하면 표 잠금 대기열에 서서 그 뒤의 읽기를 모두 막는다.
SCHEMA_CHANGE_LOCK = 732_004_150
# AWS migration Job의 600초 deadline(platform migration-jobs.yaml) 안에 끝나도록 잡았다. 넘기면 포기하고 다음 적용이 다시 만든다.
_ONLINE_INDEX_BUILD_TIMEOUT = "180s"
_ONLINE_INDEX_LOCK_TIMEOUT = "30s"


def grant_converter_access(connection) -> None:
    """converter 전용 role에 사용량 원장 기록과 변환 작업 journal 권한만 준다. 스키마 소유자 연결에서 호출한다.

    usage_ledger.start_call은 INSERT만, finish_call의 UPDATE는 WHERE id와
    status·finished_at CASE 식에서 세 컬럼을 읽으므로 이 세 컬럼만 SELECT를 준다.
    UPDATE는 finish_call이 SET하는 컬럼으로 제한해 귀속 컬럼(run_id·workspace_id·user_id)을 바꾸지 못하게 한다.
    finish_call의 SET 목록이 바뀌면 _CONVERTER_UPDATE_COLUMNS도 함께 바꾼다.
    변환 worker는 작업 journal(`ai_task_runs`)에 convert_batch run을 등록·완료·취소 확인하므로 그 테이블의
    SELECT·INSERT·UPDATE와, journal이 읽는 `ai_task_changes`·`ai_purged_scopes`의 SELECT를 준다(ADR 0034).
    convert_batch는 업무 테이블을 바꾸지 않아 변경 기록(`ai_task_changes`)에 쓰지 않는다.
    """
    role = os.environ.get("AI_DB_CONVERTER_ROLE", "").strip()
    if not role:
        logger.warning("AI_DB_CONVERTER_ROLE 미설정: converter 원장 권한 부여를 건너뜁니다.")
        return
    identifier = sql.Identifier(role)
    # GRANT는 누적되므로 이전에 준 테이블 단위 UPDATE를 먼저 회수한다(컬럼 UPDATE도 함께 회수돼 반복 적용해도 같다).
    connection.execute(sql.SQL("REVOKE UPDATE ON ai_model_usage FROM {}").format(identifier))
    connection.execute(sql.SQL("GRANT INSERT ON ai_model_usage TO {}").format(identifier))
    connection.execute(
        sql.SQL("GRANT UPDATE ({}) ON ai_model_usage TO {}").format(
            sql.SQL(", ").join(map(sql.Identifier, _CONVERTER_UPDATE_COLUMNS)), identifier)
    )
    connection.execute(
        sql.SQL("GRANT SELECT (id, status, finished_at) ON ai_model_usage TO {}").format(identifier)
    )
    connection.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON ai_task_runs TO {}").format(identifier))
    connection.execute(sql.SQL("GRANT SELECT ON ai_task_changes, ai_purged_scopes TO {}").format(identifier))


def apply_schema(connection, ddl: str) -> None:
    """스키마를 connection의 트랜잭션에 적용한다. 기다릴 때는 표가 아니라 advisory lock에서 기다린다."""
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_CHANGE_LOCK,))
    connection.execute(ddl)
    grant_converter_access(connection)


def create_online_indexes(migration_url: str) -> list[str]:
    """ONLINE_INDEXES를 잠금 없이 만들고, 만들지 못한 이름을 돌려준다. 실패해도 예외를 올려 배포를 멈추지 않는다."""
    with psycopg.connect(migration_url, autocommit=True) as connection:
        connection.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(_ONLINE_INDEX_LOCK_TIMEOUT)))
        connection.execute(sql.SQL("SET statement_timeout = {}").format(sql.Literal(_ONLINE_INDEX_BUILD_TIMEOUT)))
        try:
            connection.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_CHANGE_LOCK,))
        except psycopg.errors.LockNotAvailable:
            logger.warning("다른 인스턴스가 스키마를 바꾸는 중이라 인덱스 빌드를 건너뜁니다.")
            return [name for name in sorted(ONLINE_INDEXES) if not _is_valid_index(connection, name)]
        return [name for name in sorted(ONLINE_INDEXES) if not _create_online_index(connection, name)]


def _create_online_index(connection, name: str) -> bool:
    try:
        if _is_valid_index(connection, name):
            return True
        _drop_invalid_index(connection, name)
        connection.execute(
            sql.SQL("CREATE INDEX CONCURRENTLY {} ON {}").format(sql.Identifier(name), sql.SQL(ONLINE_INDEXES[name]))
        )
        return True
    except psycopg.Error as exc:
        logger.warning("인덱스 빌드 실패: name=%s type=%s", name, type(exc).__name__)
    try:
        _drop_invalid_index(connection, name)  # 실패한 빌드의 잔해는 쓰기 비용만 늘린다.
    except psycopg.Error as exc:
        logger.warning("실패한 인덱스 정리 실패: name=%s type=%s. 다음 적용이 다시 지운다.", name, type(exc).__name__)
    return False


def _is_valid_index(connection, name: str) -> bool:
    row = connection.execute("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(%s)", (name,)).fetchone()
    return bool(row and row[0])


def _drop_invalid_index(connection, name: str) -> None:
    if connection.execute("SELECT 1 FROM pg_index WHERE indexrelid = to_regclass(%s) AND NOT indisvalid", (name,)).fetchone():
        connection.execute(sql.SQL("DROP INDEX CONCURRENTLY {}").format(sql.Identifier(name)))


def main() -> None:
    migration_url = os.environ.get("AI_DB_MIGRATION_URL", "").strip()
    if not migration_url:
        raise ValueError("필수 migration 설정 누락: AI_DB_MIGRATION_URL")
    schema_path = Path(__file__).resolve().parents[4] / "db" / "ai_schema.sql"
    with psycopg.connect(migration_url) as connection:
        apply_schema(connection, schema_path.read_text(encoding="utf-8"))
    missing = create_online_indexes(migration_url)
    if missing:
        print(f"ai_db 인덱스 미생성(삭제 동작에는 영향 없음, docs/script.md 참고): {', '.join(missing)}")
    print("ai_db migration 완료")


if __name__ == "__main__":
    main()
