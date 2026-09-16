"""ingest command Kafka worker.

backend가 `ai.ingest.command` topic에 발행한 문서/채팅 Wiki ingest 명령을 소비해
기존 HTTP 실행 경로(`_run_pipeline_request`)를 그대로 재사용한다.

동시성 모델:
- 같은 document_id는 같은 partition → 문서별 순서 보장, 문서 간 병렬.
- worker 프로세스당 1건씩 처리(max_poll_records=1). 처리량은 worker 수 × partition으로 늘린다.
- LLM ingest는 분 단위 작업이라 max_poll_interval을 30분으로 늘려 리밸런싱을 막는다.
  (heartbeat는 aiokafka 백그라운드 태스크가 유지하고, 실행은 to_thread로 이벤트 루프를 비워 둔다)

전달 보장:
- 수동 커밋: 처리(성공/실패 기록)가 끝난 뒤에만 offset을 커밋한다.
  worker가 중간에 죽으면 재전달되고, 이미 종료된 run은 멱등하게 건너뛴다.
- 실패한 run의 상태를 pipeline_runs에 기록한 뒤 결과 event를 발행한다.
"""

from __future__ import annotations

from app.core.pipeline_control import PipelineRunCancelledError

import asyncio
import json
import logging
import os
import signal
from uuid import NAMESPACE_URL, uuid5

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from pydantic import ValidationError

from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)
from app.modules.wiki_ingestion.interfaces.http.dependencies import (
    get_deferred_pipeline_run_use_case,
    get_pipeline_run_repository,
    get_pipeline_source_reader,
)
from app.modules.wiki_ingestion.interfaces.http.routes import _run_pipeline_request
from app.modules.wiki_ingestion.interfaces.http.schemas import (
    ChatWikiRunIn,
    PipelineRunIn,
)
from app.workers.event_request import without_top_level_secrets

logger = logging.getLogger("ingest_worker")

TOPIC = os.environ.get("INGEST_COMMAND_TOPIC", "ai.ingest.command")
BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
GROUP_ID = os.environ.get("INGEST_CONSUMER_GROUP", "ingest-worker")
RESULT_TOPIC = os.environ.get("AI_TASK_EVENT_TOPIC", "ai.task.event")
POST_INGEST_TOPIC = os.environ.get(
    "MAINTENANCE_COMMAND_TOPIC",
    "ai.maintenance.command",
)
TERMINAL_STATUSES = {"succeeded", "failed", "notify_pending"}
# ingest 1건이 분 단위라 poll 간격 한도를 넉넉히 둔다. 이 시간을 넘기면 리밸런싱된다.
MAX_POLL_INTERVAL_MS = int(os.environ.get("INGEST_MAX_POLL_INTERVAL_MS", "1800000"))


class UnprocessableIngestCommand(ValueError):
    """다시 읽어도 성공할 수 없는 command.

    형식이 맞지 않아 payload를 만들지 못한 경우다. 재시도하면 같은 지점에서 다시 실패해
    offset이 전진하지 않고 파티션 전체가 막히므로, 실패로 확정하고 넘어간다.
    (예: 배포 전환기에 남은 구 형식 command)
    """


class PostIngestDispatchError(RuntimeError):
    """후속 command 발행 전에는 원본 ingest offset을 커밋하면 안 된다."""


def _build_payload(command: dict) -> PipelineRunIn | ChatWikiRunIn:
    if not command.get("user_id") or not command.get("workspace_id"):
        raise UnprocessableIngestCommand("ingest command requires user_id and workspace_id")
    if not str(command.get("provider") or "").strip() or not str(command.get("model") or "").strip():
        raise UnprocessableIngestCommand("ingest command requires provider and model")
    try:
        common = {
            "document_id": command["document_id"],
            "user_id": command.get("user_id"),
            "workspace_id": command.get("workspace_id"),
            "operation_id": command.get("operation_id"),
            "source_revision": command.get("source_revision"),
            "source_content_hash": command.get("source_content_hash"),
            "model": str(command["model"]).strip(),
            "provider": str(command.get("provider") or "").strip(),
            "wait": True,
        }
        if command.get("kind") == "chat_wiki":
            return ChatWikiRunIn(
                **common,
                selection_mode=command.get("selection_mode") or "partial",
                input_markdown=command.get("input_markdown"),
                input_blocks=command.get("input_blocks") or [],
            )
        return PipelineRunIn(**common)
    except (ValidationError, KeyError) as exc:
        # KeyError는 command에 필수 키가 없는 경우다. 둘 다 재시도로는 못 고친다.
        raise UnprocessableIngestCommand(
            f"ingest command payload is invalid (kind={command.get('kind')}): {exc!r}"
        ) from exc


def _result_payload(command: dict, manifest: dict | None, status: str, error: str | None = None) -> dict:
    return {
        "operation_id": command.get("operation_id"),
        "operation_type": "ingest",
        "status": status,
        "workspace_id": command.get("workspace_id"),
        "user_id": command.get("user_id"),
        "target_document_id": command.get("document_id"),
        "summary": error or "Wiki ingest를 완료했습니다.",
        "changed_pages": (manifest or {}).get("operation_artifacts", []),
    }


def _event(command: dict, status: str, payload: dict | None = None, error: str | None = None) -> dict:
    run_id = str(command.get("run_id") or "")
    return {
        "event_id": f"ingest:{run_id}:{status}",
        "run_id": run_id,
        "kind": "ingest" if command.get("kind") != "document_deleted" else "document_cleanup",
        "status": status,
        "workspace_id": command.get("workspace_id"),
        "user_id": command.get("user_id"),
        "operation_id": command.get("operation_id"),
        "request": without_top_level_secrets(command),
        "payload": payload,
        "error": error,
    }


def _result_event(command: dict, result: dict) -> dict:
    if result.get("status") == "failed":
        error = str(result.get("summary") or "Wiki ingest에 실패했습니다.")
        return _event(command, "failed", payload=result, error=error)
    return _event(command, "succeeded", payload=result)


def _post_ingest_command(command: dict) -> dict | None:
    if command.get("kind") == "document_deleted":
        return None
    run_id = str(command.get("run_id") or "")
    run = get_pipeline_run_repository().get_run(run_id)
    if not run or run.get("status") != "succeeded":
        return None
    if not isinstance((run.get("manifest") or {}).get("post_ingest"), dict):
        return None
    return {
        "run_id": str(uuid5(NAMESPACE_URL, f"fruition:post-ingest:{run_id}")),
        "ingest_run_id": run_id,
        "kind": "post_ingest",
        "document_id": command.get("document_id"),
        "workspace_id": command.get("workspace_id"),
        "user_id": command.get("user_id"),
        "provider": command.get("provider"),
        "model": command.get("model"),
    }


async def _dispatch_post_ingest(
    producer: AIOKafkaProducer,
    command: dict,
) -> None:
    try:
        post_ingest_command = await asyncio.to_thread(
            _post_ingest_command,
            command,
        )
        if post_ingest_command is not None:
            from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
            await asyncio.to_thread(journal.register, post_ingest_command)
            await producer.send_and_wait(
                POST_INGEST_TOPIC,
                post_ingest_command,
                key=str(post_ingest_command["workspace_id"]).encode("utf-8"),
            )
    except PipelineRunCancelledError:
        return
    except Exception as exc:
        raise PostIngestDispatchError(str(exc)) from exc



def _handle_controlled(command: dict) -> dict:
    from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
    if command.get("kind") == "document_deleted":
        return _handle(command)
    _build_payload(command)
    return journal.execute(command, lambda: _handle(command))

def _handle(command: dict) -> dict:
    if command.get("kind") == "document_deleted":
        database.delete_document_wiki_data(command["workspace_id"], command["document_id"])
        return {}

    run_id = command["run_id"]
    repository = get_pipeline_run_repository()
    existing = repository.get_run(run_id)
    if existing and any(
        existing.get(field) is not None
        and str(existing[field]) != str(command.get(field))
        for field in ("document_id", "user_id", "workspace_id")
    ):
        error = "ingest command does not match the registered run context"
        if existing.get("status") not in TERMINAL_STATUSES:
            repository.fail(run_id, error)
        raise ValueError(error)
    if existing and existing.get("status") in TERMINAL_STATUSES:
        logger.info(
            "[ingest 재전달 무시] run_id=%s 이미 종료됨 status=%s",
            run_id,
            existing.get("status"),
        )
        manifest = existing.get("manifest") or {}
        return _result_payload(command, manifest, str(existing.get("status")), existing.get("error"))
    payload = _build_payload(command)
    logger.info(
        "[ingest 실행 시작] run_id=%s kind=%s document_id=%s workspace_id=%s",
        run_id, command.get("kind"), command.get("document_id"), command.get("workspace_id"),
    )
    try:
        response = _run_pipeline_request(
            payload,
            background_tasks=None,
            use_case=get_deferred_pipeline_run_use_case(),
            repository=repository,
            source_reader=get_pipeline_source_reader(),
            run_id=run_id,
        )
    except Exception as exc:
        repository.fail(run_id, str(exc))
        raise
    logger.info("[ingest 실행 완료] run_id=%s", run_id)
    return _result_payload(command, response.manifest, "succeeded")


async def consume() -> None:
    # ai_db 스키마 준비 보장 (api 기동 경로와 동일한 공용 부트스트랩)
    database.ensure_ai_schema()
    consumer = AIOKafkaConsumer(
        TOPIC,
        bootstrap_servers=BOOTSTRAP_SERVERS,
        group_id=GROUP_ID,
        enable_auto_commit=False,
        max_poll_records=1,
        max_poll_interval_ms=MAX_POLL_INTERVAL_MS,
        auto_offset_reset="earliest",
        value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=BOOTSTRAP_SERVERS,
        enable_idempotence=True,
        value_serializer=lambda value: json.dumps(value, ensure_ascii=False).encode("utf-8"),
    )
    await consumer.start()
    await producer.start()
    logger.info("[worker 기동] topic=%s group=%s bootstrap=%s", TOPIC, GROUP_ID, BOOTSTRAP_SERVERS)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
            await asyncio.to_thread(journal.rollback_pending)
            batch = await consumer.getmany(timeout_ms=1000, max_records=1)
            for _tp, messages in batch.items():
                for message in messages:
                    try:
                        # 실행은 스레드로 넘겨 이벤트 루프(heartbeat)를 비워 둔다.
                        result = await asyncio.to_thread(_handle_controlled, message.value)
                        await _dispatch_post_ingest(producer, message.value)
                        event = _result_event(message.value, result)
                    except PostIngestDispatchError:
                        logger.exception(
                            "[post-ingest command 발행 실패] run_id=%s offset_commit=false",
                            message.value.get("run_id")
                            if isinstance(message.value, dict)
                            else "?",
                        )
                        raise
                    except UnprocessableIngestCommand as exc:
                        # 재시도해도 같은 지점에서 실패한다. 실패로 확정하고 offset을 전진시켜
                        # 이 command 하나가 파티션 전체를 막지 않게 한다.
                        run_id = message.value.get("run_id") if isinstance(message.value, dict) else None
                        logger.error("[ingest command 폐기] run_id=%s error=%s", run_id, exc)
                        if run_id:
                            try:
                                # 이미 등록된 run이면 실패로 남긴다(미등록이면 아무 일도 하지 않는다).
                                get_pipeline_run_repository().fail(run_id, str(exc))
                            except Exception:
                                logger.exception("[ingest command 폐기] run 실패 기록 실패 run_id=%s", run_id)
                        event = _event(
                            message.value,
                            "failed",
                            payload=_result_payload(message.value, None, "failed", str(exc)),
                            error=str(exc)[:1000],
                        )
                    except Exception as exc:
                        logger.exception(
                            "[ingest 실행 실패] run_id=%s",
                            message.value.get("run_id") if isinstance(message.value, dict) else "?",
                        )
                        run_id = message.value.get("run_id") if isinstance(message.value, dict) else None
                        try:
                            durable = get_pipeline_run_repository().get_run(run_id) if run_id else None
                        except Exception:
                            durable = None
                        from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
                        with journal.connect() as conn:
                            controlled = conn.execute("SELECT status FROM ai_task_runs WHERE id = %s", (run_id,)).fetchone()
                        if controlled and controlled["status"] in {*journal.STOPPING, "failed"}:
                            durable = {"status": "failed", "manifest": {}}
                        if durable is None or durable.get("status") not in TERMINAL_STATUSES:
                            # terminal run까지 못 남긴 실패는 offset을 전진시키지 않고 컨테이너 재시작으로 재수신한다.
                            raise
                        event = _event(
                            message.value,
                            "failed",
                            payload=_result_payload(message.value, durable.get("manifest"), "failed", str(exc)),
                            error=str(exc)[:1000],
                        )
                    await producer.send_and_wait(
                        RESULT_TOPIC, event, key=event["run_id"].encode("utf-8")
                    )
                    await consumer.commit()
    finally:
        await producer.stop()
        await consumer.stop()
        logger.info("[worker 종료]")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(consume())


if __name__ == "__main__":
    main()
