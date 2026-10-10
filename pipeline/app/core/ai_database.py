"""ai_db 연결. converter 이미지에서도 import하므로 psycopg 외 무거운 의존성을 두지 않는다."""
import os

import psycopg
from psycopg.rows import dict_row

from app.core.pipeline_control import task_run_id


def ai_database_url() -> str:
    url = os.environ.get("AI_DATABASE_URL")
    if not url:
        raise RuntimeError("Set AI_DATABASE_URL before using ai_db-backed APIs")
    return url


def connect_ai() -> psycopg.Connection:
    """ai-svc 소유 테이블의 ai_db 연결."""
    conn = psycopg.connect(ai_database_url(), row_factory=dict_row)
    if task_run_id.get() is not None:
        try:
            conn.execute("SELECT set_config('app.ai_task_run_id', %s, true)", (task_run_id.get(),))
        except Exception:
            conn.close()
            raise
    return conn
