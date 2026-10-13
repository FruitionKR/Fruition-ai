"""PDF 변환 Kafka command worker. `ai.convert.command`의 convert_batch를 한 번에 하나씩 처리한다."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import threading
from io import BytesIO
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from minio.error import S3Error

import converter_app
from app.core.failure_codes import failure_code
from app.core.kafka_security import kafka_security_options
from app.core.pipeline_control import ScopePurgedError, request_deadline
from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
from app.modules.wiki_ingestion.infrastructure.object_storage import bucket_name, client

logger = logging.getLogger("converter_worker")
BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
COMMAND_TOPIC = os.environ.get("CONVERT_COMMAND_TOPIC", "ai.convert.command")
RESULT_TOPIC = os.environ.get("AI_TASK_EVENT_TOPIC", "ai.task.event")
GROUP_ID = os.environ.get("CONVERT_CONSUMER_GROUP", f"{COMMAND_TOPIC}-worker")
# command 하나(재시도를 위한 범위 분할 포함) 전체의 마감이다.
BATCH_DEADLINE_SECONDS = int(os.environ.get("CONVERT_BATCH_DEADLINE_SECONDS", "7200"))
# 마감을 넘긴 시도의 종료·정리와 결과 저장에 쓰는 여유. max_poll_interval은 마감 + 이 여유보다 길어야 리밸런스되지 않는다.
POLL_MARGIN_SECONDS = 600
MAX_POLL_INTERVAL_MS = (BATCH_DEADLINE_SECONDS + POLL_MARGIN_SECONDS) * 1000
# 취소 요청을 실행 중인 변환에 전달하는 DB 확인 주기(초).
CANCEL_POLL_SECONDS = 2
MAX_PAGES_PER_COMMAND = 50
# document가 올린 원본의 object key(`sources/documents/{documentId}/original`)만 읽는다. key에는 workspace가 없다.
SOURCE_KEY = re.compile(r"sources/documents/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}/original")
SAFE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


def parse_command(command: dict[str, Any]) -> dict[str, Any]:
    """command를 검증한다. 잘못된 값은 ValueError이며 input_rejected로 알린다."""
    for field in ("run_id", "parent_run_id", "workspace_id", "user_id", "document_id", "source_key", "provider", "model"):
        if not isinstance(command.get(field), str) or not command[field].strip():
            raise ValueError(f"convert command requires: {field}")
    if command.get("kind") != "convert_batch":
        raise ValueError("unsupported convert command kind")
    for field in ("workspace_id", "document_id"):
        if not SAFE_ID.fullmatch(command[field]):
            raise ValueError(f"invalid {field}")
    if not SOURCE_KEY.fullmatch(command["source_key"]):
        raise ValueError("source_key is outside the document source prefix")
    start, count = command.get("start_page"), command.get("page_count")
    if type(start) is not int or start < 0:
        raise ValueError("start_page must be a non-negative integer")
    if type(count) is not int or not 1 <= count <= MAX_PAGES_PER_COMMAND:
        raise ValueError(f"page_count must be between 1 and {MAX_PAGES_PER_COMMAND}")
    return command


def result_key(command: dict[str, Any]) -> str:
    return f"tmp/convert/{command['workspace_id']}/{command['document_id']}/{command['start_page']}.md"


def convert_batch(command: dict[str, Any], cancelled: threading.Event) -> dict[str, Any]:
    """원본 PDF에서 command의 쪽 범위를 변환해 Markdown을 object storage에 쓰고 결과 위치를 돌려준다."""
    command = parse_command(command)
    if missing := converter_app.missing_commands():
        raise RuntimeError(f"Missing converter commands: {', '.join(missing)}")
    storage, bucket = client(), bucket_name()
    try:
        size = storage.stat_object(bucket, command["source_key"]).size
    except S3Error as error:
        if error.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            raise ValueError("source object does not exist") from error
        raise
    stream = converter_app.StorageRangeReader(storage, bucket, command["source_key"], size, cancelled)
    try:
        reader, total = converter_app.open_pdf(stream)
        start = command["start_page"]
        if start >= total:
            raise ValueError("start_page is beyond the last page")
        end = min(start + command["page_count"], total)
        # 사용량은 document가 정산하는 변환 run(parent_run_id)에 귀속한다.
        actor = {"run_id": command["parent_run_id"], "workspace_id": command["workspace_id"],
                 "user_id": command["user_id"]}
        markdown = converter_app.convert_pages(reader, start, end, command["provider"], command["model"],
                                               cancelled, actor)
    finally:
        stream.close()
    key = result_key(command)
    data = markdown.encode("utf-8")
    # 재전달로 다시 쓰면 같은 key를 같은 내용으로 덮어쓴다. 취소로 남은 object는 tmp/convert/ lifecycle이 지운다.
    storage.put_object(bucket, key, BytesIO(data), length=len(data), content_type="text/markdown; charset=utf-8")
    return {"document_id": command["document_id"], "page_start": start + 1, "page_end": end,
            "total_pages": total, "result_key": key}


def run_command(command: dict[str, Any]) -> dict[str, Any]:
    """journal에 등록해 한 번만 실행한다. 재전달되면 저장된 결과를 돌려주고 변환하지 않는다."""

    def handle() -> dict[str, Any]:
        run_id = str(command["run_id"])
        cancelled, finished = threading.Event(), threading.Event()

        def watch_cancellation() -> None:
            # POST /internal/ai/tasks/{run_id}/cancel이 run을 cancel_requested로 바꾸면 하위 프로세스를 종료시킨다.
            while not finished.wait(CANCEL_POLL_SECONDS):
                try:
                    if not journal.active(run_id):
                        cancelled.set()
                        return
                except Exception as error:
                    logger.warning("취소 확인 실패: run_id=%s error_type=%s", run_id, type(error).__name__)

        watcher = threading.Thread(target=watch_cancellation, name=f"cancel-watch-{run_id}", daemon=True)
        watcher.start()
        try:
            with request_deadline(BATCH_DEADLINE_SECONDS):
                return convert_batch(command, cancelled)
        finally:
            finished.set()
            watcher.join()

    return journal.execute(command, handle)


def _event(command: dict[str, Any], status: str, payload: Any = None, error: str | None = None) -> dict[str, Any]:
    """task_worker의 `_event`와 같은 봉투다. 원본 command(`request`)는 싣지 않는다."""
    run_id = str(command.get("run_id") or "")
    kind = str(command.get("kind") or "unknown")
    return {
        "event_id": f"{kind}:{run_id}:{status}",
        "run_id": run_id,
        "kind": kind,
        "status": status,
        "workspace_id": command.get("workspace_id"),
        "user_id": command.get("user_id"),
        "operation_id": command.get("operation_id"),
        "payload": payload,
        "error": error,
    }


def _failure_is_durable(command: dict[str, Any]) -> bool:
    """실패가 journal에 확정됐는지 본다. 아니면 커밋하지 않고 재전달에 맡긴다."""
    try:
        with journal.connect() as conn:
            row = conn.execute("SELECT status FROM ai_task_runs WHERE id = %s", (str(command.get("run_id")),)).fetchone()
        return row is not None and row["status"] in {*journal.STOPPING, "failed", "completed"}
    except Exception:
        return False


async def consume() -> None:
    consumer = AIOKafkaConsumer(
        COMMAND_TOPIC,
        bootstrap_servers=BOOTSTRAP_SERVERS,
        **kafka_security_options(),
        group_id=GROUP_ID,
        enable_auto_commit=False,
        max_poll_records=1,
        max_poll_interval_ms=MAX_POLL_INTERVAL_MS,
        auto_offset_reset="earliest",
        value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=BOOTSTRAP_SERVERS,
        **kafka_security_options(),
        enable_idempotence=True,
        value_serializer=lambda value: json.dumps(value, ensure_ascii=False).encode("utf-8"),
    )
    await consumer.start()
    await producer.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("[converter worker 기동] topic=%s group=%s", COMMAND_TOPIC, GROUP_ID)
    try:
        while not stop.is_set():
            batch = await consumer.getmany(timeout_ms=1000, max_records=1)
            for _tp, messages in batch.items():
                for message in messages:
                    # JSON object가 아닌 값은 빈 command로 두어 아래 malformed 경로가 실패로 확정한다.
                    command = message.value if isinstance(message.value, dict) else {}
                    try:
                        result = await asyncio.to_thread(run_command, command)
                        event = _event(command, "succeeded", payload=result)
                    except journal.TaskAlreadyExecutingError:
                        # 리밸런스로 재전달된 실행 중 run이다. 해당 offset으로 되돌아가 원래 worker가 끝나기를 기다린다.
                        logger.info("[실행 중 run 재전달 대기] run_id=%s offset=%s", command.get("run_id"), message.offset)
                        consumer.seek(TopicPartition(message.topic, message.partition), message.offset)
                        await asyncio.sleep(journal.REDELIVERY_BACKOFF_SECONDS)
                        break
                    except Exception as exc:
                        logger.error("[convert command 실패] run_id=%s error_type=%s", command.get("run_id"), type(exc).__name__)
                        # journal에 등록하지 못한 잘못된 command(필수 필드 없음, kind 불일치)는 재전달해도 같으므로
                        # 실패로 확정한다. 그러지 않으면 journal 행이 없어 커밋하지 못하고 같은 offset에서 계속 죽는다.
                        malformed = command.get("kind") != "convert_batch" or not all(
                            isinstance(command.get(key), str) and command[key] for key in ("run_id", "workspace_id", "user_id"))
                        if not malformed and not isinstance(exc, ScopePurgedError) and not _failure_is_durable(command):
                            raise
                        event = _event(command, "failed", error=failure_code(exc) if not malformed else "input_rejected")
                    await producer.send_and_wait(RESULT_TOPIC, event, key=event["run_id"].encode("utf-8"))
                    await consumer.commit()
    finally:
        await producer.stop()
        await consumer.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(consume())


if __name__ == "__main__":
    main()
