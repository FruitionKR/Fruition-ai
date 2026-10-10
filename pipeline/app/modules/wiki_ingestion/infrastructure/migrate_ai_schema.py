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


def grant_converter_access(connection) -> None:
    """converter 전용 role에 사용량 원장 기록 권한만 준다. 스키마 소유자 연결에서 호출한다.

    usage_ledger.start_call은 INSERT만, finish_call의 UPDATE는 WHERE id와
    status·finished_at CASE 식에서 세 컬럼을 읽으므로 이 세 컬럼만 SELECT를 준다.
    UPDATE는 finish_call이 SET하는 컬럼으로 제한해 귀속 컬럼(run_id·workspace_id·user_id)을 바꾸지 못하게 한다.
    finish_call의 SET 목록이 바뀌면 _CONVERTER_UPDATE_COLUMNS도 함께 바꾼다.
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


def main() -> None:
    migration_url = os.environ.get("AI_DB_MIGRATION_URL", "").strip()
    if not migration_url:
        raise ValueError("필수 migration 설정 누락: AI_DB_MIGRATION_URL")
    schema_path = Path(__file__).resolve().parents[4] / "db" / "ai_schema.sql"
    with psycopg.connect(migration_url) as connection:
        connection.execute(schema_path.read_text(encoding="utf-8"))
        grant_converter_access(connection)
    print("ai_db migration 완료")


if __name__ == "__main__":
    main()
