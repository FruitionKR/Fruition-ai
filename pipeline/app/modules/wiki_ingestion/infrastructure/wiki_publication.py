from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.ai_database import ai_database_url
from app.core.pipeline_control import ScopePurgedError
from app.modules.wiki_ingestion.infrastructure.object_storage import (
    create_or_verify_text_object,
)
from app.modules.wiki_ingestion.infrastructure.wiki_publication_receipt import read_receipt


EVENT_TYPE = "ai.wiki.publication.v1"
MANUAL_SUMMARY = "Wiki 페이지 본문을 직접 수정했습니다."
logger = logging.getLogger(__name__)


class PublicationConflict(RuntimeError):
    pass


def connect() -> psycopg.Connection:
    return psycopg.connect(ai_database_url(), row_factory=dict_row)


def publication_id(operation_id: str) -> str:
    return f"wiki:{operation_id}"


def object_key(workspace_id: str, page_id: str, operation_id: str, extension: str = "md") -> str:
    return f"wiki/{workspace_id}/pages/{page_id}/ops/{operation_id}.{extension}"


def prepare_publication(
    operation_id: str,
    workspace_id: str,
    user_id: str,
    kind: str,
    *,
    database_connect: Callable[[], Any] | None = None,
) -> str:
    identity = {
        "operation_id": operation_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "kind": kind,
    }
    request_hash = _sha256(_canonical_bytes(identity))
    identifier = publication_id(operation_id)
    with (database_connect or connect)() as conn:
        _lock_publication(conn, identifier)
        _lock_scope_writes(conn, workspace_id, user_id)
        _reject_purged(conn, workspace_id, user_id)
        row = conn.execute(
            """
            INSERT INTO wiki_publications (
                publication_id, operation_id, workspace_id, user_id, kind, request_hash, state
            ) VALUES (%s, %s, %s, %s, %s, %s, 'PREPARED')
            ON CONFLICT (publication_id) DO NOTHING
            RETURNING publication_id
            """,
            (identifier, operation_id, workspace_id, user_id, kind, request_hash),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                "SELECT operation_id, workspace_id, user_id, kind, request_hash, state "
                "FROM wiki_publications WHERE publication_id = %s",
                (identifier,),
            ).fetchone()
            if existing is None or {
                key: existing[key]
                for key in ("operation_id", "workspace_id", "user_id", "kind", "request_hash")
            } != {
                "operation_id": operation_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "kind": kind,
                "request_hash": request_hash,
            }:
                raise PublicationConflict(f"publication identity conflict: {identifier}")
            if existing["state"] == "ABANDONED":
                raise PublicationConflict(f"publication was abandoned: {identifier}")
    return identifier


def committed_publication_result(
    operation_id: str,
    *,
    database_connect: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    with (database_connect or connect)() as conn:
        row = conn.execute(
            "SELECT result FROM wiki_publications WHERE publication_id = %s AND state = 'COMMITTED'",
            (publication_id(operation_id),),
        ).fetchone()
    return None if row is None else dict(row["result"])


def write_publication_text(
    operation_id: str,
    page_id: str,
    key: str,
    text: str,
    content_type: str = "text/markdown; charset=utf-8",
    *,
    database_connect: Callable[[], Any] | None = None,
) -> str:
    identifier = publication_id(operation_id)
    content_hash = "sha256:" + _sha256(text.encode("utf-8"))
    with (database_connect or connect)() as conn:
        _lock_publication(conn, identifier)
        publication = conn.execute(
            "SELECT workspace_id, user_id, state FROM wiki_publications WHERE publication_id = %s FOR SHARE",
            (identifier,),
        ).fetchone()
        if publication is None:
            raise PublicationConflict(f"publication was not prepared: {identifier}")
        if publication["state"] == "ABANDONED":
            raise PublicationConflict(f"publication was abandoned: {identifier}")
        _reject_purged(conn, publication["workspace_id"], publication["user_id"])
        existing = None
        if publication["state"] == "PREPARED":
            existing = conn.execute(
                """
                INSERT INTO wiki_publication_objects (
                    publication_id, page_id, object_key, content_hash, content_type
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (publication_id, object_key) DO NOTHING
                RETURNING object_key
                """,
                (identifier, page_id, key, content_hash, content_type),
            ).fetchone()
        if existing is None:
            stored = conn.execute(
                "SELECT page_id, content_hash, content_type FROM wiki_publication_objects "
                "WHERE publication_id = %s AND object_key = %s",
                (identifier, key),
            ).fetchone()
            if stored is None or dict(stored) != {
                "page_id": page_id,
                "content_hash": content_hash,
                "content_type": content_type,
            }:
                raise PublicationConflict(f"publication object conflict: {key}")
    with _user_write_fence(
        str(publication["workspace_id"]), str(publication["user_id"]), identifier,
        database_connect=database_connect,
    ):
        uri = create_or_verify_text_object(key, text, content_type)
        with (database_connect or connect)() as conn:
            publication = conn.execute(
                "SELECT workspace_id, user_id, state FROM wiki_publications "
                "WHERE publication_id = %s FOR SHARE",
                (identifier,),
            ).fetchone()
            if publication is None:
                raise PublicationConflict(f"publication disappeared: {identifier}")
            if publication["state"] == "ABANDONED":
                raise PublicationConflict(f"publication was abandoned: {identifier}")
            _reject_purged(conn, publication["workspace_id"], publication["user_id"])
            conn.execute(
                "UPDATE wiki_publication_objects SET uploaded_at = COALESCE(uploaded_at, clock_timestamp()) "
                "WHERE publication_id = %s AND object_key = %s",
                (identifier, key),
            )
    return uri


def normalize_operation_result(result: dict[str, Any]) -> dict[str, Any]:
    changed_pages = [
        {
            "page_id": item.get("page_id"),
            "page_type": item.get("page_type"),
            "markdown_key": item.get("markdown_key"),
            "contribution_key": item.get("contribution_key"),
            "content_hash": item.get("content_hash"),
            "contribution_stored": item.get("contribution_stored"),
        }
        for item in result.get("changed_pages") or []
    ]
    failed_pages = [
        {"page_id": item.get("page_id"), "reason": item.get("reason")}
        for item in result.get("failed_pages") or []
    ]
    links = result.get("link_changes")
    if links is not None:
        links = {
            "removed_links": [_link(item) for item in links.get("removed_links") or []],
            "restored_links": [_link(item) for item in links.get("restored_links") or []],
        }
    failed_actions = [
        {
            "action": item.get("action"),
            "resource_id": item.get("resource_id"),
            "reason": item.get("reason"),
        }
        for item in result.get("failed_actions") or []
    ]
    ledger = [
        {
            "page_id": item.get("page_id"),
            "operation_id": item.get("operation_id"),
            "document_id": item.get("document_id"),
            "contribution_key": item.get("contribution_key"),
            "linked_by_operation_id": item.get("linked_by_operation_id"),
        }
        for item in result.get("ledger_contributions") or []
    ]
    status = str(result.get("status") or "")
    failure = status in {"failed", "partially_succeeded"} or bool(failed_pages or failed_actions)
    return {
        "operation_id": result.get("operation_id"),
        "operation_type": result.get("operation_type"),
        "status": status,
        "workspace_id": result.get("workspace_id"),
        "user_id": result.get("user_id"),
        "target_document_id": result.get("target_document_id"),
        "summary": result.get("summary"),
        "changed_pages": changed_pages,
        "failed_pages": failed_pages,
        "deleted_pages": list(result.get("deleted_pages") or []),
        "link_changes": links,
        "failed_actions": failed_actions or None,
        "ledger_contributions": ledger,
        "failure": failure,
    }


def finalize_publication(
    conn: psycopg.Connection,
    *,
    operation_id: str,
    workspace_id: str,
    user_id: str,
    kind: str,
    outcome: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    identifier = publication_id(operation_id)
    _lock_publication(conn, identifier)
    _lock_scope_writes(conn, workspace_id, user_id)
    _reject_purged(conn, workspace_id, user_id)
    normalized = normalize_operation_result(result)
    result_hash = _sha256(_canonical_bytes(normalized))
    reference_hash = f"sha256:{result_hash}"
    existing = conn.execute(
        "SELECT state, outcome, result_hash, result FROM wiki_publications WHERE publication_id = %s FOR UPDATE",
        (identifier,),
    ).fetchone()
    if existing is None:
        raise PublicationConflict(f"publication was not prepared: {identifier}")
    if existing["state"] == "ABANDONED":
        raise PublicationConflict(f"publication was abandoned: {identifier}")
    if existing["state"] == "COMMITTED":
        if existing["result_hash"] != result_hash or existing["outcome"] != outcome:
            raise PublicationConflict(f"publication result conflict: {identifier}")
        stored = conn.execute(
            "SELECT payload FROM wiki_publication_outbox WHERE publication_id = %s",
            (identifier,),
        ).fetchone()
        if stored is None:
            raise PublicationConflict(f"committed publication has no outbox event: {identifier}")
        return dict(stored["payload"])
    missing = conn.execute(
        "SELECT object_key FROM wiki_publication_objects WHERE publication_id = %s AND uploaded_at IS NULL LIMIT 1",
        (identifier,),
    ).fetchone()
    if missing is not None:
        raise RuntimeError(f"publication object was not uploaded: {missing['object_key']}")
    committed_at = conn.execute("SELECT clock_timestamp() AS value").fetchone()["value"]
    event = {
        "event_id": f"publication:{identifier}",
        "event_type": EVENT_TYPE,
        "publication_id": identifier,
        "operation_id": operation_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "kind": kind,
        "outcome": outcome,
        "result": normalized,
        "published_reference_hash": reference_hash,
        "committed_at": _instant(committed_at),
    }
    updated = conn.execute(
        """
        UPDATE wiki_publications
        SET state = 'COMMITTED', outcome = %s, result = %s, result_hash = %s,
            published_reference_hash = %s, committed_at = %s
        WHERE publication_id = %s AND state = 'PREPARED'
        RETURNING publication_id
        """,
        (outcome, Jsonb(normalized), result_hash, reference_hash, committed_at, identifier),
    ).fetchone()
    if updated is None:
        raise PublicationConflict(f"publication commit conflict: {identifier}")
    inserted = conn.execute(
        """
        INSERT INTO wiki_publication_outbox(event_id, publication_id, payload)
        VALUES (%s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id
        """,
        (event["event_id"], identifier, Jsonb(event)),
    ).fetchone()
    if inserted is None:
        stored = conn.execute(
            "SELECT publication_id, payload FROM wiki_publication_outbox WHERE event_id = %s",
            (event["event_id"],),
        ).fetchone()
        if stored is None or stored["publication_id"] != identifier or dict(stored["payload"]) != event:
            raise PublicationConflict(f"publication outbox conflict: {event['event_id']}")
    return event


def pending_events(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT event_id, publication_id, payload
            FROM wiki_publication_outbox outbox
            JOIN wiki_publications publication USING (publication_id)
            WHERE outbox.state = 'pending' AND outbox.next_attempt_at <= now()
              AND NOT EXISTS (
                  SELECT 1 FROM ai_purged_scopes
                  WHERE (scope_type = 'workspace' AND scope_id = publication.workspace_id)
                     OR (scope_type = 'user' AND scope_id = publication.user_id)
              )
            ORDER BY outbox.created_at
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def sent_events(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT outbox.event_id, outbox.publication_id, outbox.payload
            FROM wiki_publication_outbox outbox
            JOIN wiki_publications publication USING (publication_id)
            WHERE outbox.state = 'sent' AND outbox.receiver_disposition IS NULL
              AND outbox.next_attempt_at <= now()
              AND NOT EXISTS (
                  SELECT 1 FROM ai_purged_scopes
                  WHERE (scope_type = 'workspace' AND scope_id = publication.workspace_id)
                     OR (scope_type = 'user' AND scope_id = publication.user_id)
              )
            ORDER BY outbox.created_at
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_event_sent(event_id: str) -> None:
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE wiki_publication_outbox outbox
            SET state = 'sent', attempts = attempts + 1, sent_at = now()
            FROM wiki_publications publication
            WHERE outbox.publication_id = publication.publication_id
              AND outbox.event_id = %s AND outbox.state = 'pending'
              AND NOT EXISTS (
                  SELECT 1 FROM ai_purged_scopes
                  WHERE (scope_type = 'workspace' AND scope_id = publication.workspace_id)
                     OR (scope_type = 'user' AND scope_id = publication.user_id)
              )
            RETURNING outbox.event_id
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                "SELECT state FROM wiki_publication_outbox WHERE event_id = %s",
                (event_id,),
            ).fetchone()
            if existing is not None and existing["state"] == "sent":
                return
            raise ScopePurgedError("Publication relay scope was purged.")


def mark_event_retry(event_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE wiki_publication_outbox SET attempts = attempts + 1, "
            "next_attempt_at = now() + interval '5 seconds' WHERE event_id = %s AND state = 'pending'",
            (event_id,),
        )


def reconcile_sent_receipts(
    limit: int = 20,
    *,
    receipt_reader: Callable[[dict[str, str]], dict[str, Any] | None] = read_receipt,
) -> int:
    """Persist exact receiver dispositions; unknown delivery is queued with the same event."""
    confirmed = 0
    for row in sent_events(limit):
        event_id = str(row["event_id"])
        publication_identifier = str(row["publication_id"])
        payload = dict(row["payload"])
        identity = {
            "workspace_id": str(payload["workspace_id"]),
            "user_id": str(payload["user_id"]),
            "publication_id": str(payload["publication_id"]),
            "operation_id": str(payload["operation_id"]),
            "event_id": event_id,
        }
        try:
            receipt = receipt_reader(identity)
            disposition, rejection_reason = (
                _validate_receipt(payload, receipt) if receipt is not None else (None, None)
            )
        except Exception:
            logger.warning(
                "Wiki publication receipt is unknown; retrying the same event: event_id=%s",
                event_id,
                exc_info=True,
            )
            receipt = None
            disposition = rejection_reason = None
        try:
            with _user_write_fence(
                identity["workspace_id"], identity["user_id"], publication_identifier
            ):
                if receipt is None:
                    logger.info(
                        "Wiki publication receipt is not committed yet; retrying the same event: "
                        "event_id=%s",
                        event_id,
                    )
                    _queue_event_redelivery(event_id)
                    continue
                assert disposition is not None
                if not _mark_receiver_receipt(event_id, disposition, rejection_reason):
                    continue
        except (ScopePurgedError, PublicationConflict):
            continue
        confirmed += 1
        if disposition == "REJECTED_CONFLICT":
            logger.error(
                "Wiki publication receiver rejected committed state; operator action required: "
                "publication_id=%s operation_id=%s reason=%s",
                publication_identifier,
                payload["operation_id"],
                rejection_reason,
            )
    return confirmed


def _queue_event_redelivery(event_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE wiki_publication_outbox SET state = 'pending', "
            "next_attempt_at = now() + interval '5 seconds' "
            "WHERE event_id = %s AND state = 'sent' AND receiver_disposition IS NULL",
            (event_id,),
        )


def _mark_receiver_receipt(
    event_id: str, disposition: str, rejection_reason: str | None
) -> bool:
    with connect() as conn:
        return conn.execute(
            """
            UPDATE wiki_publication_outbox
            SET receiver_disposition = %s, receiver_rejection_reason = %s,
                receiver_confirmed_at = clock_timestamp()
            WHERE event_id = %s AND state = 'sent' AND receiver_disposition IS NULL
            RETURNING event_id
            """,
            (disposition, rejection_reason, event_id),
        ).fetchone() is not None


def _validate_receipt(
    event: dict[str, Any], receipt: dict[str, Any]
) -> tuple[str, str | None]:
    exact = {
        "publication_id": event["publication_id"],
        "operation_id": event["operation_id"],
        "event_id": event["event_id"],
        "workspace_id": event["workspace_id"],
        "user_id": event["user_id"],
        "kind": event["kind"],
        "outcome": event["outcome"],
        "published_reference_hash": event["published_reference_hash"],
    }
    if any(receipt.get(key) != value for key, value in exact.items()):
        raise ValueError("publication receipt identity does not match the immutable event")
    try:
        event_time = datetime.fromisoformat(str(event["committed_at"]).replace("Z", "+00:00"))
        receipt_time = datetime.fromisoformat(str(receipt["committed_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise ValueError("publication receipt committed_at is invalid") from exc
    if event_time != receipt_time:
        raise ValueError("publication receipt committed_at does not match the immutable event")
    disposition = receipt.get("disposition")
    rejection_reason = receipt.get("rejection_reason")
    if disposition == "ACCEPTED" and rejection_reason is None:
        return disposition, None
    if (disposition == "REJECTED_CONFLICT" and rejection_reason == "STALE_RESTORE"
            and event["kind"] in {"RESTORE", "RECOVERY"}
            and event["outcome"] == "PUBLISHED"):
        return disposition, rejection_reason
    raise ValueError("publication receipt disposition is invalid")


async def relay_pending(producer: Any, topic: str, limit: int = 20) -> int:
    """Relay committed publications at least once; sent is not receiver acceptance."""
    sent = 0
    for row in pending_events(limit):
        event_id = str(row["event_id"])
        payload = dict(row["payload"])
        try:
            with _user_write_fence(
                str(payload["workspace_id"]), str(payload["user_id"]), str(row["publication_id"])
            ):
                await producer.send_and_wait(
                    topic,
                    payload,
                    key=str(payload["operation_id"]).encode("utf-8"),
                )
                mark_event_sent(event_id)
        except Exception:
            mark_event_retry(event_id)
            raise
        sent += 1
    return sent


def _reject_purged(conn: psycopg.Connection, workspace_id: str, user_id: str) -> None:
    if conn.execute(
        """
        SELECT 1 FROM ai_purged_scopes
        WHERE (scope_type = 'workspace' AND scope_id = %s)
           OR (scope_type = 'user' AND scope_id = %s)
        """,
        (workspace_id, user_id),
    ).fetchone():
        raise ScopePurgedError("Publication scope was purged.")


def _lock_scope_writes(conn: psycopg.Connection, workspace_id: str, user_id: str) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
        ("ai-workspace-write:" + str(workspace_id),),
    )
    conn.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
        ("ai-user-write:" + str(user_id),),
    )


def _lock_publication(conn: psycopg.Connection, identifier: str) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("ai-wiki-publication:" + identifier,),
    )


@contextmanager
def _user_write_fence(
    workspace_id: str,
    user_id: str,
    identifier: str,
    *,
    database_connect: Callable[[], Any] | None = None,
):
    with (database_connect or connect)() as conn:
        _lock_publication(conn, identifier)
        _lock_scope_writes(conn, workspace_id, user_id)
        _reject_purged(conn, workspace_id, user_id)
        publication = conn.execute(
            "SELECT state FROM wiki_publications WHERE publication_id = %s", (identifier,)
        ).fetchone()
        if publication is None:
            raise PublicationConflict(f"publication disappeared: {identifier}")
        if publication["state"] == "ABANDONED":
            raise PublicationConflict(f"publication was abandoned: {identifier}")
        yield


def _link(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": item.get("source"),
        "target": item.get("target"),
        "relation": item.get("relation"),
    }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
