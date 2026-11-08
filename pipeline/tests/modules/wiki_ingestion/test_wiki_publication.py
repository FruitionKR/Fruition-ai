from __future__ import annotations

import hashlib
import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import psycopg
import pytest
from minio.error import S3Error
from psycopg.rows import dict_row

from app.core.pipeline_control import ScopePurgedError
from app.modules.data_purge.infrastructure import postgres_purge as purge
from app.modules.wiki_ingestion.infrastructure import wiki_publication as publication
from app.modules.wiki_ingestion.infrastructure import wiki_publication_cleanup as cleanup
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as repository
from app.modules.wiki_ingestion.infrastructure.object_storage import (
    ImmutableObjectConflict,
    read_text_object,
    storage_uri,
    write_text_object,
)
from app.modules.wiki_ingestion.infrastructure.pending_contributions import (
    record_pending_deltas,
    with_pending_contributions,
)
from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import concept_write_lock


GOLDEN_HASH = "0f1a3e22db4b09ab4ee73f63a14e3ae8f35aca082af86eb21d3c9f2ba474424c"


def _connect():
    return psycopg.connect(os.environ["AI_DATABASE_URL"], row_factory=dict_row)


@pytest.fixture(autouse=True)
def clean_publication_state():
    if not os.environ.get("AI_DATABASE_URL"):
        pytest.skip("AI_DATABASE_URL is required")
    with _connect() as conn:
        conn.execute("TRUNCATE wiki_publication_outbox, wiki_publication_objects, wiki_publications, wiki_pending_contributions, wiki_pages, ai_task_changes, ai_task_runs, ai_purged_scopes CASCADE")


def _result(operation_id: str, workspace_id: str = "ws", user_id: str = "user") -> dict:
    return publication.normalize_operation_result({
        "operation_id": operation_id,
        "operation_type": "lint",
        "status": "succeeded",
        "workspace_id": workspace_id,
        "user_id": user_id,
        "target_document_id": None,
        "summary": "Wiki lint로 페이지 1개를 변경했습니다.",
        "changed_pages": [],
        "failed_pages": [],
        "deleted_pages": [],
        "link_changes": None,
        "failed_actions": None,
        "ledger_contributions": [],
    })


def _mark_abandoned(operation_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE wiki_publications SET state = 'ABANDONED', abandoned_at = now() "
            "WHERE operation_id = %s AND state = 'PREPARED'",
            (operation_id,),
        )


def _commit_no_change(
    operation_id: str,
    workspace_id: str = "ws",
    user_id: str = "user",
    *,
    kind: str = "LINT",
    outcome: str = "NO_CHANGE",
) -> dict:
    result = _result(operation_id, workspace_id, user_id)
    if kind in {"RESTORE", "RECOVERY"}:
        result = {**result, "operation_type": "restore"}
    publication.prepare_publication(operation_id, workspace_id, user_id, kind)
    with _connect() as conn:
        return publication.finalize_publication(
            conn, operation_id=operation_id, workspace_id=workspace_id, user_id=user_id,
            kind=kind, outcome=outcome, result=result,
        )


def _receipt(event: dict, disposition: str = "ACCEPTED", reason: str | None = None) -> dict:
    return {
        key: event[key]
        for key in (
            "publication_id", "operation_id", "event_id", "workspace_id", "user_id",
            "kind", "outcome", "published_reference_hash", "committed_at",
        )
    } | {"disposition": disposition, "rejection_reason": reason}


class _RecordingProducer:
    def __init__(self):
        self.events: list[dict] = []

    async def send_and_wait(self, _topic, payload, key):
        self.events.append(dict(payload))


def test_full_typed_result_matches_document_golden_fixture() -> None:
    fixture = Path(__file__).parents[2] / "resources/wiki-publication-lint-result-canonical-v1.json"
    raw = fixture.read_bytes()
    assert hashlib.sha256(raw.rstrip(b"\n")).hexdigest() == GOLDEN_HASH
    value = json.loads(raw)
    normalized = publication.normalize_operation_result(value)
    canonical = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
    assert canonical == raw.rstrip(b"\n")
    assert hashlib.sha256(canonical).hexdigest() == GOLDEN_HASH

    document_root = os.environ.get("DOCUMENT_FIXTURE_ROOT")
    if document_root:
        document_fixture = Path(document_root) / "wiki-publication-lint-result-canonical-v1.json"
        assert document_fixture.read_bytes().rstrip(b"\n") == raw.rstrip(b"\n")


def test_entry_relink_json_is_registered_under_committed_publication() -> None:
    operation_id = "op-entry-relink-publication"
    page_id = "page-entry-relink"
    linked = {
        "schema_version": 1,
        "artifact_type": "lint",
        "operation_id": "op-linked",
        "document_id": "lint:op-linked",
        "page_id": page_id,
        "concept": {},
        "evidence_units": [],
        "links": [],
    }
    result = {
        "_lint_page_changes": [],
        "_entry_state": {
            "linked": [linked],
            "pages": [],
            "link_changes": {"removed_links": [], "restored_links": []},
            "deleted_pages": [],
        },
    }
    publication.prepare_publication(operation_id, "ws", "user", "LINT")

    def write_artifact(key: str, text: str, content_type: str) -> str:
        return publication.write_publication_text(
            operation_id, page_id, key, text, content_type, database_connect=_connect,
        )

    with _connect() as conn:
        repository.persist_lint_operation_result(
            "ws", operation_id, result, connection=conn, artifact_writer=write_artifact,
        )
        event = publication.finalize_publication(
            conn,
            operation_id=operation_id,
            workspace_id="ws",
            user_id="user",
            kind="LINT",
            outcome="PUBLISHED",
            result=publication.normalize_operation_result({
                **_result(operation_id),
                "ledger_contributions": result["ledger_contributions"],
            }),
        )
    key = f"wiki/ws/pages/{page_id}/ops/op-linked.json"
    body = read_text_object(key)
    with _connect() as conn:
        stored = conn.execute(
            "SELECT publication_id, page_id, content_hash, content_type, uploaded_at IS NOT NULL AS uploaded "
            "FROM wiki_publication_objects WHERE object_key = %s",
            (key,),
        ).fetchone()
        payload = conn.execute(
            "SELECT payload FROM wiki_publication_outbox WHERE publication_id = %s",
            (publication.publication_id(operation_id),),
        ).fetchone()["payload"]
    assert json.loads(body) == linked
    assert stored == {
        "publication_id": publication.publication_id(operation_id),
        "page_id": page_id,
        "content_hash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        "content_type": "application/json; charset=utf-8",
        "uploaded": True,
    }
    assert event == payload


def test_upload_first_rollback_then_atomic_commit_and_replay() -> None:
    operation_id = "op-atomic"
    key = publication.object_key("ws", "page-1", operation_id)
    publication.prepare_publication(operation_id, "ws", "user", "LINT")
    publication.write_publication_text(operation_id, "page-1", key, "new body")
    publication.write_publication_text(operation_id, "page-1", key, "new body")
    with pytest.raises((publication.PublicationConflict, ImmutableObjectConflict)):
        publication.write_publication_text(operation_id, "page-1", key, "different body")

    with _connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,status,markdown_uri,user_id,workspace_id,created_at,updated_at) "
            "VALUES ('page-1','concept','Page','page','active','s3://fruition-storage/old.md','user','ws',now(),now())"
        )

    tx = _connect()
    tx.execute("UPDATE wiki_pages SET markdown_uri = %s WHERE id = 'page-1'", (f"s3://fruition-storage/{key}",))
    event = publication.finalize_publication(
        tx, operation_id=operation_id, workspace_id="ws", user_id="user",
        kind="LINT", outcome="PUBLISHED", result=_result(operation_id),
    )
    with _connect() as observer:
        assert observer.execute("SELECT markdown_uri FROM wiki_pages WHERE id='page-1'").fetchone()["markdown_uri"].endswith("old.md")
        assert observer.execute("SELECT 1 FROM wiki_publication_outbox").fetchone() is None
    tx.rollback()
    tx.close()
    with _connect() as conn:
        assert conn.execute("SELECT state FROM wiki_publications WHERE operation_id=%s", (operation_id,)).fetchone()["state"] == "PREPARED"
        assert conn.execute("SELECT uploaded_at IS NOT NULL AS uploaded FROM wiki_publication_objects").fetchone()["uploaded"]

    with _connect() as conn:
        conn.execute("UPDATE wiki_pages SET markdown_uri = %s WHERE id = 'page-1'", (f"s3://fruition-storage/{key}",))
        committed = publication.finalize_publication(
            conn, operation_id=operation_id, workspace_id="ws", user_id="user",
            kind="LINT", outcome="PUBLISHED", result=_result(operation_id),
        )
    with _connect() as conn:
        assert conn.execute("SELECT markdown_uri FROM wiki_pages WHERE id='page-1'").fetchone()["markdown_uri"].endswith(key)
        replay = publication.finalize_publication(
            conn, operation_id=operation_id, workspace_id="ws", user_id="user",
            kind="LINT", outcome="PUBLISHED", result=_result(operation_id),
        )
    assert committed["result"] == event["result"]
    assert committed["published_reference_hash"] == event["published_reference_hash"]
    assert replay == committed


def test_multipage_failure_blocks_commit_then_outbox_retries(monkeypatch) -> None:
    publication.prepare_publication("op-many", "ws", "user", "LINT")
    first = publication.object_key("ws", "page-1", "op-many")
    second = publication.object_key("ws", "page-2", "op-many")
    publication.write_publication_text("op-many", "page-1", first, "one")
    create = publication.create_or_verify_text_object

    def fail_second(key: str, text: str, content_type: str):
        if key == second:
            raise OSError("injected upload failure")
        return create(key, text, content_type)

    monkeypatch.setattr(publication, "create_or_verify_text_object", fail_second)
    with pytest.raises(OSError, match="injected"):
        publication.write_publication_text("op-many", "page-2", second, "two")
    with _connect() as conn, pytest.raises(RuntimeError, match="not uploaded"):
        publication.finalize_publication(
            conn, operation_id="op-many", workspace_id="ws", user_id="user",
            kind="LINT", outcome="PUBLISHED", result=_result("op-many"),
        )
    monkeypatch.setattr(publication, "create_or_verify_text_object", create)
    publication.write_publication_text("op-many", "page-2", second, "two")
    with _connect() as conn:
        publication.finalize_publication(
            conn, operation_id="op-many", workspace_id="ws", user_id="user",
            kind="LINT", outcome="PUBLISHED", result=_result("op-many"),
        )

    class Producer:
        def __init__(self):
            self.fail = True
            self.events = []

        async def send_and_wait(self, _topic, payload, key):
            if self.fail:
                self.fail = False
                raise OSError("broker response lost")
            self.events.append((payload, key))

    producer = Producer()
    with pytest.raises(OSError, match="response lost"):
        asyncio.run(publication.relay_pending(producer, "ai.task.event"))
    with _connect() as conn:
        state = conn.execute("SELECT state, attempts FROM wiki_publication_outbox").fetchone()
        assert state == {"state": "pending", "attempts": 1}
        conn.execute("UPDATE wiki_publication_outbox SET next_attempt_at=now()")
    assert asyncio.run(publication.relay_pending(producer, "ai.task.event")) == 1
    with _connect() as conn:
        assert conn.execute("SELECT state FROM wiki_publication_outbox").fetchone()["state"] == "sent"


def test_receiver_receipt_acceptance_is_durable_across_restarts() -> None:
    event = _commit_no_change("op-receiver-accepted")
    producer = _RecordingProducer()
    assert asyncio.run(publication.relay_pending(producer, "ai.task.event")) == 1

    assert publication.reconcile_sent_receipts(
        receipt_reader=lambda identity: _receipt(event)
    ) == 1
    with _connect() as conn:
        assert conn.execute(
            "SELECT state, receiver_disposition, receiver_rejection_reason, "
            "receiver_confirmed_at IS NOT NULL AS confirmed FROM wiki_publication_outbox"
        ).fetchone() == {
            "state": "sent", "receiver_disposition": "ACCEPTED",
            "receiver_rejection_reason": None, "confirmed": True,
        }
    assert publication.reconcile_sent_receipts(
        receipt_reader=lambda _identity: pytest.fail("confirmed receipt must not be fetched again")
    ) == 0


def test_receiver_commit_then_broker_response_loss_recovers_by_exact_duplicate() -> None:
    event = _commit_no_change("op-receiver-response-loss")

    class CommitThenLoseResponse(_RecordingProducer):
        def __init__(self):
            super().__init__()
            self.receipt = None

        async def send_and_wait(self, _topic, payload, key):
            self.events.append(dict(payload))
            if self.receipt is None:
                self.receipt = _receipt(payload)
                raise OSError("broker response lost after receiver commit")

    producer = CommitThenLoseResponse()
    with pytest.raises(OSError, match="response lost"):
        asyncio.run(publication.relay_pending(producer, "ai.task.event"))
    with _connect() as conn:
        assert conn.execute("SELECT state FROM wiki_publication_outbox").fetchone()["state"] == "pending"
        conn.execute("UPDATE wiki_publication_outbox SET next_attempt_at=now()")
    assert asyncio.run(publication.relay_pending(producer, "ai.task.event")) == 1
    assert [item["event_id"] for item in producer.events] == [event["event_id"], event["event_id"]]
    assert publication.reconcile_sent_receipts(
        receipt_reader=lambda _identity: producer.receipt
    ) == 1


def test_missing_timeout_and_mismatched_receipt_retry_the_same_event() -> None:
    event = _commit_no_change("op-receiver-unknown")
    producer = _RecordingProducer()
    asyncio.run(publication.relay_pending(producer, "ai.task.event"))

    assert publication.reconcile_sent_receipts(receipt_reader=lambda _identity: None) == 0
    with _connect() as conn:
        row = conn.execute(
            "SELECT state, attempts, receiver_disposition FROM wiki_publication_outbox"
        ).fetchone()
        assert row == {"state": "pending", "attempts": 1, "receiver_disposition": None}
        conn.execute("UPDATE wiki_publication_outbox SET next_attempt_at = now()")
    asyncio.run(publication.relay_pending(producer, "ai.task.event"))
    assert [item["event_id"] for item in producer.events] == [event["event_id"], event["event_id"]]

    mismatched = _receipt(event) | {"workspace_id": "other-workspace"}
    assert publication.reconcile_sent_receipts(receipt_reader=lambda _identity: mismatched) == 0
    with _connect() as conn:
        assert conn.execute(
            "SELECT state, receiver_disposition FROM wiki_publication_outbox"
        ).fetchone() == {"state": "pending", "receiver_disposition": None}
        conn.execute("UPDATE wiki_publication_outbox SET state='sent', next_attempt_at=now()")

    wrong_hash = _receipt(event) | {"published_reference_hash": "sha256:" + "0" * 64}
    assert publication.reconcile_sent_receipts(receipt_reader=lambda _identity: wrong_hash) == 0
    with _connect() as conn:
        assert conn.execute(
            "SELECT state, receiver_disposition FROM wiki_publication_outbox"
        ).fetchone() == {"state": "pending", "receiver_disposition": None}
        conn.execute("UPDATE wiki_publication_outbox SET state='sent', next_attempt_at=now()")

    def timeout(_identity):
        raise TimeoutError("response lost")

    assert publication.reconcile_sent_receipts(receipt_reader=timeout) == 0
    with _connect() as conn:
        assert conn.execute("SELECT state FROM wiki_publication_outbox").fetchone()["state"] == "pending"


def test_rejected_receipt_is_durable_and_blocks_affected_pending_rebuild() -> None:
    event = _commit_no_change(
        "op-receiver-rejected", kind="RESTORE", outcome="PUBLISHED"
    )
    with _connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,status,user_id,workspace_id,created_at,updated_at) "
            "VALUES ('page-rejected','concept','Page','page-rejected','active','user','ws',now(),now())"
        )
        record_pending_deltas(conn, "ws", [{
            "page_id": "page-rejected", "operation_id": "op-receiver-rejected",
            "document_id": "doc", "object_key": "wiki/ws/pages/page-rejected/ops/op.json",
            "active": True, "sequence_revision": 1,
        }], publication_id=event["publication_id"])
    asyncio.run(publication.relay_pending(_RecordingProducer(), "ai.task.event"))

    assert publication.reconcile_sent_receipts(
        receipt_reader=lambda _identity: _receipt(event, "REJECTED_CONFLICT", "STALE_RESTORE")
    ) == 1
    exact = [{
        "page_id": "page-rejected", "ingest_operation_id": "op-receiver-rejected",
        "source_document_id": "doc", "sequence_revision": 1, "active": True,
        "object_key": "wiki/ws/pages/page-rejected/ops/op.json", "lint_operation_id": None,
    }]
    with _connect() as conn, pytest.raises(RuntimeError, match="rejected by receiver"):
        with_pending_contributions(conn, exact, ["page-rejected"])


def test_purge_before_delayed_receipt_lookup_cannot_resurrect_delivery_state() -> None:
    _commit_no_change("op-receiver-purged", "ws-purged", "user-purged")
    asyncio.run(publication.relay_pending(_RecordingProducer(), "ai.task.event"))
    purge.purge_user("user-purged")

    assert publication.reconcile_sent_receipts(
        receipt_reader=lambda _identity: pytest.fail("purged delivery must not be looked up")
    ) == 0
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM wiki_publications").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM wiki_publication_outbox").fetchone()["n"] == 0


def test_actual_subprocess_death_leaves_retryable_inflight() -> None:
    script = """
from app.modules.wiki_ingestion.infrastructure import wiki_publication as p
p.prepare_publication('op-death','ws','user','LINT')
p.write_publication_text('op-death','page-death',p.object_key('ws','page-death','op-death'),'body')
import os
os._exit(23)
"""
    completed = subprocess.run([sys.executable, "-c", script], env=os.environ.copy(), check=False)
    assert completed.returncode == 23
    with _connect() as conn:
        row = conn.execute(
            "SELECT publication.state, object.uploaded_at IS NOT NULL AS uploaded "
            "FROM wiki_publications publication JOIN wiki_publication_objects object USING(publication_id) "
            "WHERE publication.operation_id='op-death'"
        ).fetchone()
        assert row == {"state": "PREPARED", "uploaded": True}
        event = publication.finalize_publication(
            conn, operation_id="op-death", workspace_id="ws", user_id="user",
            kind="LINT", outcome="NO_CHANGE", result=_result("op-death"),
        )
    assert event["outcome"] == "NO_CHANGE"


def test_terminal_task_reclaims_uploaded_object_and_fences_late_writer(monkeypatch) -> None:
    command = {
        "run_id": "00000000-0000-0000-0000-000000000903",
        "kind": "lint",
        "operation_id": "op-abandoned",
        "workspace_id": "ws",
        "user_id": "user",
    }
    key = publication.object_key("ws", "page-abandoned", command["operation_id"])

    def fail_after_upload():
        publication.prepare_publication(command["operation_id"], "ws", "user", "LINT")
        publication.write_publication_text(
            command["operation_id"], "page-abandoned", key, "abandoned body"
        )
        raise OSError("injected terminal failure")

    with pytest.raises(OSError, match="terminal failure"):
        journal.execute(command, fail_after_upload)
    with _connect() as conn:
        assert conn.execute(
            "SELECT state FROM wiki_publications WHERE operation_id = %s",
            (command["operation_id"],),
        ).fetchone()["state"] == "ABANDONED"

    monkeypatch.setenv(cleanup.GRACE_SECONDS_ENV, "0")
    assert cleanup.cleanup_configured_publications() == 1
    with _connect() as conn:
        assert conn.execute(
            "SELECT deleted_at IS NOT NULL AS deleted FROM wiki_publication_objects WHERE object_key = %s",
            (key,),
        ).fetchone()["deleted"]
    with pytest.raises(S3Error) as missing:
        read_text_object(key)
    assert missing.value.code in {"NoSuchKey", "NoSuchObject"}
    with pytest.raises(publication.PublicationConflict, match="abandoned"):
        publication.write_publication_text(
            command["operation_id"], "page-abandoned", key, "abandoned body"
        )
    with _connect() as conn, pytest.raises(publication.PublicationConflict, match="abandoned"):
        publication.finalize_publication(
            conn, operation_id=command["operation_id"], workspace_id="ws", user_id="user",
            kind="LINT", outcome="NO_CHANGE", result=_result(command["operation_id"]),
        )


def test_unowned_expired_prepared_object_is_claimed_before_delete() -> None:
    operation_id = "op-unowned-expired"
    key = publication.object_key("ws", "page-unowned", operation_id)
    publication.prepare_publication(operation_id, "ws", "user", "MANUAL_EDIT")
    publication.write_publication_text(operation_id, "page-unowned", key, "orphan")

    assert cleanup.cleanup_abandoned_publications(grace_seconds=0) == 1
    with _connect() as conn:
        row = conn.execute(
            "SELECT publication.state, object.deleted_at IS NOT NULL AS deleted "
            "FROM wiki_publications publication JOIN wiki_publication_objects object USING(publication_id) "
            "WHERE publication.operation_id = %s",
            (operation_id,),
        ).fetchone()
    assert row == {"state": "ABANDONED", "deleted": True}


def test_cleanup_preserves_active_upload_and_pending_commit(monkeypatch) -> None:
    operation_id = "op-racing-commit"
    key = publication.object_key("ws", "page-race", operation_id)
    publication.prepare_publication(operation_id, "ws", "user", "LINT")
    upload_started = threading.Event()
    allow_upload = threading.Event()
    upload_finished = threading.Event()
    allow_commit = threading.Event()
    original_upload = publication.create_or_verify_text_object
    errors: list[BaseException] = []

    def blocking_upload(*args, **kwargs):
        upload_started.set()
        assert allow_upload.wait(5)
        return original_upload(*args, **kwargs)

    monkeypatch.setattr(publication, "create_or_verify_text_object", blocking_upload)

    def writer() -> None:
        try:
            with concept_write_lock("ws", operation_id):
                publication.write_publication_text(operation_id, "page-race", key, "racing body")
                upload_finished.set()
                assert allow_commit.wait(5)
                with _connect() as conn:
                    publication.finalize_publication(
                        conn, operation_id=operation_id, workspace_id="ws", user_id="user",
                        kind="LINT", outcome="NO_CHANGE", result=_result(operation_id),
                    )
        except BaseException as exc:
            errors.append(exc)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert upload_started.wait(5)
    cleanup_result: list[int] = []

    def collect_cleanup() -> None:
        try:
            cleanup_result.append(cleanup.cleanup_abandoned_publications(grace_seconds=0))
        except BaseException as exc:
            errors.append(exc)

    cleanup_thread = threading.Thread(target=collect_cleanup)
    cleanup_thread.start()
    cleanup_thread.join(0.2)
    assert cleanup_thread.is_alive()
    allow_upload.set()
    assert upload_finished.wait(5)
    cleanup_thread.join(0.2)
    assert cleanup_thread.is_alive()
    allow_commit.set()
    writer_thread.join(5)
    cleanup_thread.join(5)
    assert not writer_thread.is_alive() and not cleanup_thread.is_alive()
    assert errors == []
    assert cleanup_result == [0]
    assert cleanup.cleanup_abandoned_publications(grace_seconds=0) == 0
    assert read_text_object(key) == "racing body"


def test_cleanup_preserves_current_contribution_and_journal_recovery_references() -> None:
    operation_id = "op-retained"
    publication.prepare_publication(operation_id, "ws", "user", "LINT")
    current_key = publication.object_key("ws", "page-current", operation_id)
    contribution_key = publication.object_key("ws", "page-current", operation_id, "json")
    recovery_key = publication.object_key("ws", "page-recovery", operation_id)
    for page_id, key, body in (
        ("page-current", current_key, "current"),
        ("page-current", contribution_key, "{}"),
        ("page-recovery", recovery_key, "recovery"),
    ):
        publication.write_publication_text(operation_id, page_id, key, body)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,status,markdown_uri,user_id,workspace_id,created_at,updated_at) "
            "VALUES ('page-current','concept','Current','current','active',%s,'user','ws',now(),now()), "
            "('page-recovery','concept','Recovery','recovery','deleted',NULL,'user','ws',now(),now())",
            (storage_uri(current_key),),
        )
        record_pending_deltas(conn, "ws", [{
            "page_id": "page-current", "operation_id": operation_id, "document_id": None,
            "object_key": contribution_key, "active": True,
        }], publication_id="retained-history")
        conn.execute(
            "INSERT INTO ai_task_runs(id,workspace_id,user_id,kind,command_hash,command,status) "
            "VALUES ('retained-run','ws','user','lint','hash','{}','rollback_failed')"
        )
        conn.execute(
            "INSERT INTO ai_task_changes(run_id,table_name,row_key,before_value,after_value,undone) "
            "VALUES ('retained-run','wiki_pages','{}',%s,'{}',false)",
            (json.dumps({"markdown_uri": storage_uri(recovery_key)}),),
        )
    _mark_abandoned(operation_id)
    assert cleanup.cleanup_abandoned_publications(grace_seconds=0) == 0
    assert read_text_object(current_key) == "current"
    assert read_text_object(contribution_key) == "{}"
    assert read_text_object(recovery_key) == "recovery"


def test_failed_object_delete_remains_retryable(monkeypatch) -> None:
    operation_id = "op-delete-retry"
    key = publication.object_key("ws", "page-delete-retry", operation_id)
    publication.prepare_publication(operation_id, "ws", "user", "LINT")
    publication.write_publication_text(operation_id, "page-delete-retry", key, "body")
    _mark_abandoned(operation_id)
    original_delete = cleanup.delete_object
    monkeypatch.setattr(cleanup, "delete_object", lambda _key: (_ for _ in ()).throw(OSError("delete failed")))
    with pytest.raises(OSError, match="delete failed"):
        cleanup.cleanup_abandoned_publications(grace_seconds=0)
    with _connect() as conn:
        assert not conn.execute(
            "SELECT deleted_at IS NOT NULL AS deleted FROM wiki_publication_objects WHERE object_key = %s",
            (key,),
        ).fetchone()["deleted"]
    monkeypatch.setattr(cleanup, "delete_object", original_delete)
    assert cleanup.cleanup_abandoned_publications(grace_seconds=0) == 1


def test_commit_response_loss_recovers_task_from_committed_publication() -> None:
    command = {
        "run_id": "00000000-0000-0000-0000-000000000901",
        "kind": "lint",
        "operation_id": "op-response-loss",
        "workspace_id": "ws",
        "user_id": "user",
    }
    row = journal.register(command)
    assert journal._begin_attempt(row) is None
    publication.prepare_publication("op-response-loss", "ws", "user", "LINT")
    expected = _result("op-response-loss")
    with _connect() as conn:
        publication.finalize_publication(
            conn, operation_id="op-response-loss", workspace_id="ws", user_id="user",
            kind="LINT", outcome="NO_CHANGE", result=expected,
        )
    with journal.connect() as conn:
        interrupted = conn.execute("SELECT * FROM ai_task_runs WHERE id=%s", (command["run_id"],)).fetchone()
    assert journal._begin_attempt(interrupted) == expected
    with journal.connect() as conn:
        assert conn.execute("SELECT status FROM ai_task_runs WHERE id=%s", (command["run_id"],)).fetchone()["status"] == "completed"


def test_postcommit_side_effect_error_returns_committed_result() -> None:
    command = {
        "run_id": "00000000-0000-0000-0000-000000000902",
        "kind": "lint",
        "operation_id": "op-postcommit-error",
        "workspace_id": "ws",
        "user_id": "user",
    }
    expected = _result(command["operation_id"])

    def handle():
        publication.prepare_publication(command["operation_id"], "ws", "user", "LINT")
        with _connect() as conn:
            publication.finalize_publication(
                conn, operation_id=command["operation_id"], workspace_id="ws", user_id="user",
                kind="LINT", outcome="NO_CHANGE", result=expected,
            )
        raise OSError("response serialization failed after commit")

    assert journal.execute(command, handle) == expected
    with journal.connect() as conn:
        assert conn.execute("SELECT status FROM ai_task_runs WHERE id=%s", (command["run_id"],)).fetchone()["status"] == "completed"


def test_pending_manual_lint_and_restore_transition_is_exact() -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,status,user_id,workspace_id,created_at,updated_at) "
            "VALUES ('page-1','concept','Page','page','active','user','ws',now(),now())"
        )
        record_pending_deltas(conn, "ws", [{
            "page_id": "page-1", "operation_id": "op-manual", "document_id": None,
            "object_key": "wiki/ws/pages/page-1/ops/op-manual.json", "active": True,
            "sequence_revision": 1,
        }], publication_id="wiki:manual")
        overlaid = with_pending_contributions(conn, [], ["page-1"])
        assert overlaid[0]["source_document_id"] is None and overlaid[0]["active"]
        exact = [{
            **overlaid[0],
            "page_id": "page-1",
        }]
        assert with_pending_contributions(conn, exact, ["page-1"]) == exact

        record_pending_deltas(conn, "ws", [{
            "page_id": "page-1", "operation_id": "op-lint", "document_id": None,
            "object_key": "wiki/ws/pages/page-1/ops/op-lint.json", "active": True,
            "linked_by_operation_id": None,
        }], publication_id="wiki:lint")
        record_pending_deltas(conn, "ws", [{
            "page_id": "page-1", "operation_id": "op-lint", "document_id": None,
            "object_key": "wiki/ws/pages/page-1/ops/op-lint.json", "active": False,
            "sequence_revision": 2, "linked_by_operation_id": None,
        }], publication_id="wiki:restore")
        rows = conn.execute(
            "SELECT publication_id, active, superseded_at IS NOT NULL AS superseded "
            "FROM wiki_pending_contributions WHERE operation_id='op-lint' ORDER BY created_at"
        ).fetchall()
        assert rows == [
            {"publication_id": "wiki:lint", "active": True, "superseded": True},
            {"publication_id": "wiki:restore", "active": False, "superseded": False},
        ]
        assert not next(item for item in with_pending_contributions(conn, [], ["page-1"]) if item["ingest_operation_id"] == "op-lint")["active"]


def test_manual_edit_commits_page_result_pending_and_outbox_together(monkeypatch) -> None:
    workspace_id = "ws-manual-publication"
    old_key = f"wiki/{workspace_id}/pages/page-manual/ops/op-old.md"
    write_text_object(old_key, "# Old\n")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,status,markdown_uri,user_id,workspace_id,created_at,updated_at) "
            "VALUES ('page-manual','concept','Manual','manual','active',%s,'user',%s,now(),now())",
            (f"s3://{os.environ['S3_BUCKET']}/{old_key}", workspace_id),
        )
    monkeypatch.setattr(repository, "invalidate_concept_index", lambda *_args: None)
    body = "# Manual\n"
    changed = repository.save_manual_wiki_edit(
        "page-manual", "user", workspace_id, "op-manual-publication", body
    )
    assert changed is not None and changed["content_hash"] == "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    with _connect() as conn:
        page = conn.execute("SELECT markdown_uri FROM wiki_pages WHERE id='page-manual'").fetchone()
        event = conn.execute("SELECT payload FROM wiki_publication_outbox").fetchone()["payload"]
        pending = conn.execute(
            "SELECT document_id, active, object_key FROM wiki_pending_contributions WHERE page_id='page-manual'"
        ).fetchone()
    assert page["markdown_uri"].endswith("wiki/ws-manual-publication/pages/page-manual/ops/op-manual-publication.md")
    assert event["result"]["failure"] is False
    assert event["result"]["summary"] == publication.MANUAL_SUMMARY
    assert event["result"]["changed_pages"][0]["contribution_stored"] is True
    assert pending["document_id"] is None and pending["active"]
    assert repository.save_manual_wiki_edit(
        "page-manual", "user", workspace_id, "op-manual-publication", body
    ) == changed
    with pytest.raises(publication.PublicationConflict):
        repository.save_manual_wiki_edit(
            "page-manual", "user", workspace_id, "op-manual-publication", body + "different"
        )


def test_purge_fence_blocks_delayed_upload_and_relay() -> None:
    publication.prepare_publication("op-purge", "ws", "user", "LINT")
    with _connect() as conn:
        conn.execute("INSERT INTO ai_purged_scopes(scope_type,scope_id) VALUES ('workspace','ws')")
    with pytest.raises(ScopePurgedError):
        publication.write_publication_text(
            "op-purge", "page-1", publication.object_key("ws", "page-1", "op-purge"), "body"
        )
    assert publication.pending_events() == []


def test_upload_uses_injected_connection_for_fence_and_completion(monkeypatch) -> None:
    def unexpected_default_connection():
        raise AssertionError("publication must stay in the injected database scope")

    monkeypatch.setattr(publication, "connect", unexpected_default_connection)
    publication.prepare_publication("op-scoped", "ws", "user", "LINT", database_connect=_connect)
    key = publication.object_key("ws", "page-scoped", "op-scoped")
    publication.write_publication_text("op-scoped", "page-scoped", key, "body", database_connect=_connect)
    with _connect() as conn:
        assert conn.execute(
            "SELECT uploaded_at IS NOT NULL AS uploaded FROM wiki_publication_objects WHERE object_key = %s",
            (key,),
        ).fetchone()["uploaded"]
