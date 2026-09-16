"""runtime과 분리한 일회성 ai_db migration 명령."""

import os
from pathlib import Path

import psycopg


def main() -> None:
    migration_url = os.environ.get("AI_DB_MIGRATION_URL", "").strip()
    if not migration_url:
        raise ValueError("필수 migration 설정 누락: AI_DB_MIGRATION_URL")
    schema_path = Path(__file__).resolve().parents[4] / "db" / "ai_schema.sql"
    with psycopg.connect(migration_url) as connection:
        connection.execute(schema_path.read_text(encoding="utf-8"))
    print("ai_db migration 완료")


if __name__ == "__main__":
    main()
