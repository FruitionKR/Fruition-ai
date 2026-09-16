"""document.edit.event Kafka consumer — 파생물 stale 추적.

document-svc가 `document.edit.event` topic에 발행한 편집 저장 이벤트를 소비해
ai_db `document_derived_state`에 최신 편집 상태(revision·hash)를 upsert한다.

stale 판정은 저장하지 않고 조회 시
`ingested_hash IS DISTINCT FROM last_edit_hash`로 계산한다.

전달 보장:
- key=document_id → 같은 문서는 같은 partition에서 순차 소비.
- upsert에 revision 역행 방지 조건이 있어 재전송·순서 뒤집힘에도 멱등하다.
- 파싱 실패 메시지는 경고 로그 후 skip한다 (poison pill 무한 재시도 금지).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import datetime

from aiokafka import AIOKafkaConsumer

from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)

logger = logging.getLogger("edit_event_consumer")

TOPIC = os.environ.get("DOCUMENT_EDIT_EVENT_TOPIC", "document.edit.event")
BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
GROUP_ID = os.environ.get("DERIVED_STATE_CONSUMER_GROUP", "derived-state-tracker")

_UPSERT_SQL = """
INSERT INTO document_derived_state (
    document_id, workspace_id, last_edit_revision, last_edit_hash, last_edited_at, updated_at
)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (document_id) DO UPDATE
SET workspace_id = EXCLUDED.workspace_id,
    last_edit_revision = EXCLUDED.last_edit_revision,
    last_edit_hash = EXCLUDED.last_edit_hash,
    last_edited_at = EXCLUDED.last_edited_at,
    updated_at = now()
-- revision 역행 방지: 재전송·순서 뒤집힘 시 이전 revision으로 덮어쓰지 않는다
WHERE document_derived_state.last_edit_revision < EXCLUDED.last_edit_revision
"""


def _parse_event(raw: bytes) -> tuple[str, str, int, str, datetime]:
    """이벤트 JSON에서 upsert에 필요한 필드를 검증해 꺼낸다. 실패 시 ValueError."""
    event = json.loads(raw.decode("utf-8"))
    if not isinstance(event, dict):
        raise ValueError("event payload must be a JSON object")
    document_id = str(event["document_id"])
    workspace_id = str(event["workspace_id"])
    revision = int(event["revision"])
    content_hash = str(event["content_hash"])
    created_at = datetime.fromisoformat(str(event["created_at"]).replace("Z", "+00:00"))
    if not document_id or not workspace_id or not content_hash:
        raise ValueError("document_id, workspace_id, content_hash must be non-empty")
    return document_id, workspace_id, revision, content_hash, created_at


def _handle(raw: bytes) -> None:
    document_id, workspace_id, revision, content_hash, created_at = _parse_event(raw)
    with database.connect_ai() as conn:
        conn.execute(
            _UPSERT_SQL,
            (document_id, workspace_id, revision, content_hash, created_at),
        )
    logger.info(
        "[derived-state 갱신] document_id=%s revision=%s", document_id, revision
    )


async def consume() -> None:
    # ai_db 스키마 준비 보장 (api·ingest-worker와 동일한 공용 부트스트랩)
    database.ensure_ai_schema()
    consumer = AIOKafkaConsumer(
        TOPIC,
        bootstrap_servers=BOOTSTRAP_SERVERS,
        group_id=GROUP_ID,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    await consumer.start()
    logger.info("[consumer 기동] topic=%s group=%s bootstrap=%s", TOPIC, GROUP_ID, BOOTSTRAP_SERVERS)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            batch = await consumer.getmany(timeout_ms=1000)
            for _tp, messages in batch.items():
                for message in messages:
                    try:
                        # DB 호출은 스레드로 넘겨 이벤트 루프(heartbeat)를 비워 둔다.
                        await asyncio.to_thread(_handle, message.value)
                    except (ValueError, KeyError, json.JSONDecodeError):
                        # 형식이 깨진 메시지는 재시도해도 성공할 수 없다 — 경고 후 skip.
                        logger.warning(
                            "[이벤트 파싱 실패 — skip] offset=%s value=%r",
                            message.offset,
                            message.value[:512] if message.value else message.value,
                            exc_info=True,
                        )
            if batch:
                await consumer.commit()
    finally:
        await consumer.stop()
        logger.info("[consumer 종료]")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(consume())


if __name__ == "__main__":
    main()
