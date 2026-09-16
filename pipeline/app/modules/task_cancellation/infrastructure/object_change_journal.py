"""기존 object를 덮어쓰기 전에 원본과 예정 결과를 영속 기록한다."""

from io import BytesIO

from minio.error import S3Error
from psycopg.types.json import Jsonb

from app.core.pipeline_control import PipelineRunCancelledError
from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal


def snapshot(client, bucket: str, key: str):
    try:
        response = client.get_object(bucket, key)
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            return None
        raise
    try:
        return {"text": response.read().decode("utf-8"),
                "content_type": response.headers.get("Content-Type", "application/octet-stream")}
    finally:
        response.close()
        response.release_conn()


def apply(client, bucket: str, key: str, value):
    if value is None:
        client.remove_object(bucket, key)
        return
    data = value["text"].encode("utf-8")
    client.put_object(bucket, key, BytesIO(data), length=len(data), content_type=value["content_type"])


def change_object(run_id: str, client, bucket: str, key: str, after) -> None:
    with journal.connect() as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"ai-object:{bucket}/{key}",))
        try:
            before = snapshot(client, bucket, key)
            # 쓰기 직전 상태를 먼저 커밋한다. 프로세스가 죽어도 예정 결과와 대조해 복구할 수 있다.
            task = conn.execute("SELECT status FROM ai_task_runs WHERE id = %s FOR SHARE", (run_id,)).fetchone()
            if task is None or task["status"] != "running":
                raise PipelineRunCancelledError("Task no longer accepts object changes.")
            if before == after:
                return
            record = conn.execute("INSERT INTO ai_task_changes(run_id, table_name, row_key, before_value, after_value, applied) "
                         "VALUES (%s, '__object__', %s, %s, %s, false) RETURNING id",
                         (run_id, Jsonb({"bucket": bucket, "key": key}), Jsonb(before), Jsonb(after))).fetchone()
            conn.commit()
            apply(client, bucket, key, after)
            conn.execute("UPDATE ai_task_changes SET applied = true WHERE id = %s", (record["id"],))
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (f"ai-object:{bucket}/{key}",))


def undo_object(change) -> None:
    from app.modules.wiki_ingestion.infrastructure.object_storage import client
    storage = client()
    bucket, key = change["row_key"]["bucket"], change["row_key"]["key"]
    with journal.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"ai-object:{bucket}/{key}",))
        current = snapshot(storage, bucket, key)
        if current == change["before_value"] and not change["applied"]:
            raise ValueError("Object forward outcome is still unknown.")
        if current != change["before_value"]:
            if current != change["after_value"]:
                raise ValueError("Object changed after this task.")
            apply(storage, bucket, key, change["before_value"])
        if snapshot(storage, bucket, key) != change["before_value"]:
            raise ValueError("Object restoration verification failed.")
        conn.execute("UPDATE ai_task_changes SET undone = true WHERE id = %s", (change["id"],))
