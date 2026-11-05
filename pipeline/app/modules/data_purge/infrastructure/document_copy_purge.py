"""영구 삭제한 문서의 본문 복사본을 AI DB와 객체 저장소에서 지운다(document_purged).

휴지통 이동(document_deleted)은 복구할 수 있어 위키 파생만 정리한다. 이 모듈은 복구가 끝난 문서에만 돈다.
run·plan·operation·실행 행은 이력으로 남기고 본문이 든 값만 지운다. 한 run이 여러 문서를 건드렸어도 이 문서의 작업과
객체만 건드린다. 그래서 run 접두사가 아니라 artifact 키 단위로 지운다.

DB 값을 먼저 지우고 객체를 지운다. 객체 삭제가 실패해도 예외를 consumer로 올리지 않는다. 올리면 offset이 멈춰 같은 파티션의
ingest까지 막힌다. 요청 행(ai_document_purges)의 finished_at이 비어 있으면 agent worker가 다시 시도한다.
모든 단계가 조건부 삭제·갱신이라 재전달해도 결과가 같다. 로그에는 건수와 id만 남긴다.

소스 페이지는 이 문서 하나에서만 나온다. 그래서 본문 객체(`wiki/{ws}/sources/{document_id}.md`, `wiki/{ws}/pages/{page}/`)와
요약을 지운다. 행은 status='deleted'로 남긴다. 로그 되돌리기가 page id로 찾고 슬러그 고유 제약과 tombstone이 그대로 맞아야 하며,
위키 조회·그래프·임베딩은 활성 페이지만 읽어 영향이 없다. markdown_uri가 NULL인 삭제 페이지가 "영구 삭제함" 표시다.
개념 페이지·기여는 여러 문서의 파생이라 지우지 않는다.

클러스터 파일과 편입 로그는 여러 문서가 함께 쓰는 객체라 이 문서의 항목만 뺀다(purged_document_markdown). 작업 기록의 값도 같은
변환을 거쳐 작업 취소가 계속 맞고 본문을 되살리지 않는다. 다른 편입과 같은 개념 잠금을 잡되 오래 기다리지 않고, 못 잡으면 다음 주기로 넘긴다.

진행 중이던 run은 삭제 전에 읽은 근거를 스캔 뒤에 쓸 수 있다. 그래서 스캔 전에 그런 run이 있으면 끝내지 않고(active_runs) 다음 주기에
다시 훑는다. 빠짐없이 지운 스캔(last_scan_at) 뒤에 시작한 run은 지워진 본문을 읽을 수 없어 기다리지 않는다. 다른 문서의 run도 질의·편입·lint로 이 문서의 근거를
읽으므로 문서 id로 좁히지 않고, 위키를 읽지 않는 종류만 뺀다. 요청 뒤 하루가 지나면 그때까지 시작한 run은 나이 제한으로 이미 빠졌으므로,
겹쳐 도는 새 run을 더 기다리지 않고 그 시도의 스캔으로 끝낸다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import logging
from typing import Any

from psycopg.types.json import Json

from app.modules.agent_run.domain.plan import DOCUMENT_PURGED_ERROR_CODE
from app.modules.agent_run.infrastructure.expired_artifact_cleanup import (
    MAX_STORAGE_FAILURES_PER_CYCLE,
    _delete_if_present,
    _is_in_run_scope,
)
from app.modules.data_purge.infrastructure.purged_document_markdown import (
    without_document_clusters,
    without_document_log,
)
from app.modules.task_cancellation.infrastructure import object_change_journal, postgres_task_journal as journal
from app.modules.wiki_generation.domain.text_utils import slugify
from app.modules.wiki_ingestion.infrastructure import object_storage
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database
from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import concept_write_lock


logger = logging.getLogger(__name__)

PENDING_PURGE_BATCH_SIZE = 20
# 수정안 요약은 본문을 인용할 수 있어 지운다. 작업 목록에 이유 대신 이 문구가 보인다.
_PURGED_REASON = "영구 삭제된 문서입니다."
_CONTENT_TOOLS = ["apply_document_edit", "create_document"]
_SCRUBBED = Json({"scrubbed": True})
# command에 document_id가 있고 result가 그 문서의 편입 결과인 kind. 질의 답변은 범위 밖이라 근거 본문만 지운다.
_DOCUMENT_TASK_KINDS = ["document", "chat_wiki", "ingest", "post_ingest"]
# 다른 문서의 run에도 이 문서의 근거 문장이 들어간다. (테이블, 열, 키). 식별자는 코드 상수다.
_EVIDENCE_COLUMNS = (
    ("ai_task_runs", "result", "id"),
    ("agent_runs", "result", "id"),
    ("pipeline_runs", "manifest", "id"),
    ("document_quality_reviews", "evaluations", "document_id"),
)
_EVIDENCE_TEXT_KEYS = {"text": "", "expected_claims": []}
# 진행 중 run과 연달아 겹치면 이번 시도에서는 그 행을 넘기고 다음 주기에 다시 본다.
_EVIDENCE_UPDATE_ATTEMPTS = 3
# 근거가 든 행의 작업 기록. 비운 값과 맞도록 변경 전후 값을 같은 방식으로 비운다.
_EVIDENCE_JOURNAL_TABLES = ["pipeline_runs", "document_quality_reviews"]
# 소비자에서 도는 경로라 다른 편입이 끝나기를 오래 기다려 파티션을 막지 않는다.
_SHARED_OBJECT_LOCK_TIMEOUT = "5s"
# 끝날 때 결과를 쓸 수 있는 상태. 승인을 기다리는 agent run은 쓰지 않는다.
_ACTIVE_TASK_STATUSES = ["running", "cancel_requested", "rolling_back"]
_ACTIVE_AGENT_STATUSES = ["queued", "planning", "executing", "verifying", "cancel_requested", "rolling_back"]
# 입력이 음성·전사문·자기 PDF뿐이고 위키와 다른 문서를 읽지 않아 이 문서의 근거를 쓸 수 없는 종류. 새 종류는 기본으로 기다린다.
_TASK_KINDS_WITHOUT_DOCUMENT_TEXT = ["convert_batch", "meeting_notes", "meeting_transcription"]
# 죽은 worker의 run이 정리를 영원히 붙잡지 않게 이보다 오래된 run은 기다리지 않는다. 요청 뒤 이만큼 지나면 겹치는 새 run도 기다리지 않는다.
_ACTIVE_RUN_MAX_AGE = "1 day"


@dataclass(frozen=True)
class DocumentCopyPurge:
    deleted_artifacts: int = 0
    pending_artifacts: int = 0  # 객체 삭제에 실패해 행을 남겼다. 다음 주기에 다시 시도한다.
    pending_source_objects: int = 0  # 소스 페이지 객체 삭제에 실패했다. 다음 주기에 접두사를 다시 훑는다.
    pending_evidence: int = 0  # 근거 행이 계속 갱신되어 비우지 못했다. 다음 주기에 다시 시도한다.
    pending_shared_objects: int = 0  # 클러스터·로그 객체를 고치지 못했다. 다음 주기에 다시 시도한다.
    active_runs: int = 0  # 스캔 뒤에 결과를 쓸 수 있는 run. 끝난 다음 주기에 다시 훑는다.
    failed: bool = False  # 예기치 못한 실패로 이번 시도를 끝냈다. 다음 주기에 처음부터 다시 시도한다.
    # 결과 event로 document에 알린다. core_db에는 페이지 종류가 없어 document가 버전 본문을 지울 페이지를 스스로 가릴 수 없다.
    source_page_ids: tuple[str, ...] = ()

    @property
    def scan_complete(self) -> bool:
        """이번 시도가 찾은 복사본을 빠짐없이 지웠다. 아니면 그 뒤에 시작한 run도 남은 본문을 읽을 수 있다."""
        return (not self.failed and self.pending_artifacts == 0 and self.pending_source_objects == 0
                and self.pending_evidence == 0 and self.pending_shared_objects == 0)

    @property
    def finished(self) -> bool:
        return self.scan_complete and self.active_runs == 0


@dataclass(frozen=True)
class PendingDocumentPurges:
    finished: int = 0
    pending: int = 0


def purge_document(workspace_id: str, document_id: str) -> DocumentCopyPurge:
    # 요청을 먼저 커밋한다. 이 INSERT만 실패하면 DB 장애이므로 예외를 올려 재전달에 맡긴다.
    with database.connect_ai() as conn:
        conn.execute(
            "INSERT INTO ai_document_purges (workspace_id, document_id) VALUES (%s, %s) "
            "ON CONFLICT (workspace_id, document_id) DO UPDATE SET finished_at = NULL",
            (workspace_id, document_id),
        )
        source_page_ids = tuple(_source_page_ids(conn, workspace_id, document_id))
    return replace(_attempt(workspace_id, document_id), source_page_ids=source_page_ids)


def purge_pending_documents(limit: int = PENDING_PURGE_BATCH_SIZE) -> PendingDocumentPurges:
    """끝내지 못한 영구 삭제를 다시 시도한다. 오래 못 본 것부터 보므로 한 문서가 계속 실패해도 뒤가 밀리지 않는다."""
    with database.connect_ai() as conn:
        rows = conn.execute(
            """
            SELECT workspace_id, document_id FROM ai_document_purges
            WHERE finished_at IS NULL
            ORDER BY last_attempt_at NULLS FIRST, requested_at
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    finished = 0
    unfinished = 0
    failures = 0
    for row in rows:
        result = _attempt(row["workspace_id"], row["document_id"])
        if result.finished:
            finished += 1
            continue
        unfinished += 1
        # 저장소 장애면 객체마다 재시도까지 기다려 agent job이 함께 밀린다. run을 기다리는 것은 장애가 아니다.
        if result.scan_complete:
            continue
        failures += 1
        if failures >= MAX_STORAGE_FAILURES_PER_CYCLE:
            break
    return PendingDocumentPurges(finished=finished, pending=unfinished)


def is_source_page_purged(workspace_id: str, page_id: str) -> bool:
    """로그 되돌리기가 스냅샷을 읽기 전에 본다. 객체가 없어 실패하면 같은 작업의 개념 페이지 복구까지 막힌다."""
    with database.connect_ai() as conn:
        return conn.execute(
            """
            SELECT 1 FROM wiki_pages
            WHERE id = %s AND workspace_id = %s AND page_type = 'source'
              AND status = 'deleted' AND markdown_uri IS NULL
            """,
            (page_id, workspace_id),
        ).fetchone() is not None


def _attempt(workspace_id: str, document_id: str) -> DocumentCopyPurge:
    try:
        # 스캔 전에 센다. 스캔 뒤에 세면 그 사이에 끝난 run의 쓰기를 놓친다.
        active_runs = _count_active_runs(workspace_id, document_id)
        _retire_active_source_pages(workspace_id, document_id)
        with database.connect_ai() as conn:
            scrubbed = _scrub_rows(conn, workspace_id, document_id)
        result = _delete_artifacts(workspace_id, document_id)
        result = replace(
            result,
            pending_source_objects=_delete_source_objects(workspace_id, document_id),
            pending_evidence=scrubbed["evidence_pending"],
            pending_shared_objects=_strip_shared_objects(workspace_id, document_id),
            active_runs=active_runs,
        )
    except Exception as exc:
        # 오류 원문은 SQL 값이나 키를 담을 수 있어 타입만 남긴다.
        logger.warning("[문서 복사본 삭제 실패] workspace_id=%s document_id=%s type=%s",
                       workspace_id, document_id, type(exc).__name__)
        result = DocumentCopyPurge(failed=True)
        scrubbed = {}
    with database.connect_ai() as conn:
        conn.execute(
            "UPDATE ai_document_purges SET last_attempt_at = now(), "
            "last_scan_at = CASE WHEN %s THEN now() ELSE last_scan_at END, "
            "finished_at = CASE WHEN %s THEN now() END WHERE workspace_id = %s AND document_id = %s",
            (result.scan_complete, result.finished, workspace_id, document_id),
        )
    logger.info(
        "[문서 복사본 삭제] workspace_id=%s document_id=%s rows=%s deleted_artifacts=%s pending_artifacts=%s "
        "pending_source_objects=%s pending_shared_objects=%s active_runs=%s failed=%s",
        workspace_id, document_id, scrubbed, result.deleted_artifacts, result.pending_artifacts,
        result.pending_source_objects, result.pending_shared_objects, result.active_runs, result.failed,
    )
    return result


def _count_active_runs(workspace_id: str, document_id: str) -> int:
    """빠짐없이 지운 마지막 스캔이 끝나기 전에 시작해 아직 안 끝난 run 수. 그런 스캔이 없으면 지금까지 시작한 run을 모두 센다. 대기 상한을 넘기면 0이다."""
    with database.connect_ai() as conn:
        row = conn.execute(
            """
            WITH purge AS (
                SELECT last_scan_at, requested_at FROM ai_document_purges
                WHERE workspace_id = %(ws)s AND document_id = %(doc)s
            ), scan AS (
                SELECT COALESCE((SELECT last_scan_at FROM purge), now()) AS at
            )
            SELECT (SELECT count(*) FROM ai_task_runs, scan
                    WHERE workspace_id = %(ws)s AND status = ANY(%(tasks)s) AND NOT kind = ANY(%(no_text)s)
                      AND created_at <= scan.at AND created_at > now() - %(age)s::interval)
                 + (SELECT count(*) FROM agent_runs, scan
                    WHERE workspace_id = %(ws)s AND status = ANY(%(agents)s)
                      AND created_at <= scan.at AND created_at > now() - %(age)s::interval) AS count,
                   COALESCE((SELECT requested_at <= now() - %(age)s::interval FROM purge), false) AS expired
            """,
            {"ws": workspace_id, "doc": document_id, "tasks": _ACTIVE_TASK_STATUSES,
             "no_text": _TASK_KINDS_WITHOUT_DOCUMENT_TEXT, "agents": _ACTIVE_AGENT_STATUSES, "age": _ACTIVE_RUN_MAX_AGE},
        ).fetchone()
    if not row["expired"]:
        return row["count"]
    if row["count"]:
        # 이 시도의 스캔 뒤에 이 run들이 쓰는 근거는 더 찾지 않는다. 언제 놓칠 수 있었는지 알 수 있게 남긴다.
        logger.warning("[문서 복사본 삭제 대기 상한] workspace_id=%s document_id=%s active_runs=%s",
                       workspace_id, document_id, row["count"])
    return 0


def _source_page_ids(conn: Any, workspace_id: str, document_id: str, *, active_only: bool = False) -> list[str]:
    # 소스 페이지 슬러그는 문서 id다(upsert_wiki_page). 정규화 전후 둘 다 본다.
    return [
        row["id"]
        for row in conn.execute(
            """
            SELECT id FROM wiki_pages
            WHERE workspace_id = %s AND page_type = 'source' AND slug = ANY(%s)
              AND (NOT %s OR status <> 'deleted')
            """,
            (workspace_id, [document_id, slugify(document_id)], active_only),
        ).fetchall()
    ]


def _retire_active_source_pages(workspace_id: str, document_id: str) -> None:
    """휴지통 이동의 document_deleted가 처리되지 못했으면 링크·임베딩·tombstone까지 같은 경로로 먼저 정리한다."""
    with database.connect_ai() as conn:
        active = _source_page_ids(conn, workspace_id, document_id, active_only=True)
    if active:
        # 잠금 시간 초과는 _attempt가 실패로 기록하고 다음 주기에 다시 시도한다.
        database.delete_document_wiki_data(workspace_id, document_id)


def _delete_source_objects(workspace_id: str, document_id: str) -> int:
    """이 문서 전용 소스 페이지 객체를 지운다. 실패하면 1을 돌려준다. 키는 행이 아니라 id에서 나와 재시도 때 다시 찾을 수 있다."""
    with database.connect_ai() as conn:
        pages = _source_page_ids(conn, workspace_id, document_id)
    if not pages:
        return 0
    try:
        storage = object_storage.client()
        bucket = object_storage.bucket_name()
        keys = [f"wiki/{workspace_id}/sources/{document_id}.md"]
        keys += [
            item.object_name
            for page_id in pages
            for item in storage.list_objects(bucket, prefix=f"wiki/{workspace_id}/pages/{page_id}/", recursive=True)
        ]
        for key in keys:
            storage.remove_object(bucket, key)  # 없는 키 삭제도 S3에서는 성공이다.
    except Exception as exc:
        logger.warning("소스 페이지 객체 삭제 실패: document_id=%s type=%s", document_id, type(exc).__name__)
        return 1
    return 0


def _strip_shared_objects(workspace_id: str, document_id: str) -> int:
    """클러스터·로그 객체와 그 작업 기록에서 이 문서의 항목을 뺀다. 실패하면 1을 돌려준다. 다시 해도 결과가 같다."""
    root = f"wiki/{workspace_id}/"
    clusters = [f"{root}clusters/active.md", f"{root}clusters/archived.md"]
    logs = f"{root}logs/"

    def strip(key: str, text: str) -> str:
        return without_document_log(text, document_id) if key.startswith(logs) else without_document_clusters(text, document_id)

    try:
        storage = object_storage.client()
        bucket = object_storage.bucket_name()
        with database.connect_ai() as conn:
            changes = conn.execute(
                """
                SELECT change.id, change.row_key->>'key' AS key, change.before_value, change.after_value
                FROM ai_task_changes change
                WHERE change.run_id = ANY(%(runs)s) AND change.table_name = '__object__'
                  AND (change.row_key->>'key' = ANY(%(clusters)s) OR left(change.row_key->>'key', %(length)s) = %(logs)s)
                  AND (change.before_value::text LIKE %(doc)s OR change.after_value::text LIKE %(doc)s)
                """,
                {"runs": _workspace_run_ids(conn, workspace_id), "clusters": clusters, "logs": logs,
                 "length": len(logs), "doc": f"%{document_id}%"},
            ).fetchall()
        # 잠금 전에 고를 객체를 가린다. 로그는 날짜마다 하나라 다 읽는 동안 그 워크스페이스의 편입을 막지 않는다.
        # 잠금 밖에서 놓친 이 문서의 항목은 진행 중 run이 쓴 것이므로 active_runs가 다음 주기에 다시 보게 한다.
        keys = {change["key"] for change in changes}
        for key in [*clusters, *(item.object_name for item in storage.list_objects(bucket, prefix=logs, recursive=True))]:
            current = object_change_journal.snapshot(storage, bucket, key)
            if current is not None and strip(key, current["text"]) != current["text"]:
                keys.add(key)
        if not keys:
            return 0
        # 편입은 이 잠금 안에서 파일을 읽어 덧붙인다. 잠금 없이 쓰면 그 덧붙임이 지운 항목을 되살린다.
        with concept_write_lock(workspace_id, f"document-purge:{document_id}", _SHARED_OBJECT_LOCK_TIMEOUT):
            for key in sorted(keys):
                _strip_shared_object(storage, bucket, key, lambda text, key=key: strip(key, text),
                                     [change for change in changes if change["key"] == key])
    except Exception as exc:
        # 잠금 시간 초과를 포함한다. 오류 원문은 키를 담을 수 있어 타입만 남긴다.
        logger.warning("공유 객체 정리 실패: document_id=%s type=%s", document_id, type(exc).__name__)
        return 1
    return 0


def _strip_shared_object(storage: Any, bucket: str, key: str, strip: Any, changes: list[dict[str, Any]]) -> None:
    def stripped(value: dict[str, Any] | None) -> dict[str, Any] | None:
        return value if value is None else {**value, "text": strip(value["text"])}

    # change_object와 같은 순서로 잠근다. 작업 취소(undo_object)와도 이 객체 잠금으로 겹치지 않는다.
    with object_change_journal.write_fence(key, True), journal.connect() as conn:
        conn.execute("SELECT set_config('lock_timeout', %s, true)", (_SHARED_OBJECT_LOCK_TIMEOUT,))
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"ai-object:{bucket}/{key}",))
        current = object_change_journal.snapshot(storage, bucket, key)
        if stripped(current) != current:
            object_change_journal.apply(storage, bucket, key, stripped(current))
        for change in changes:
            conn.execute(
                "UPDATE ai_task_changes SET before_value = %s, after_value = %s WHERE id = %s",
                (Json(stripped(change["before_value"])), Json(stripped(change["after_value"])), change["id"]),
            )


def _scrub_rows(conn: Any, workspace_id: str, document_id: str) -> dict[str, int]:
    params: dict[str, Any] = {"ws": workspace_id, "doc": document_id}
    _require_same_workspace(conn, params)
    counts = _scrub_agent_operations(conn, params)
    # post_ingest 행은 document_id가 비어 있고 같은 id의 작업 command만 문서를 안다.
    params["runs"] = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM ai_task_runs WHERE workspace_id = %s AND command->>'document_id' = %s AND kind = ANY(%s)",
            (workspace_id, document_id, _DOCUMENT_TASK_KINDS),
        ).fetchall()
    ]
    counts["source_pages"] = conn.execute(
        "UPDATE wiki_pages SET summary = '', markdown_uri = NULL, updated_at = now() "
        "WHERE id = ANY(%s) AND status = 'deleted'",
        (_source_page_ids(conn, workspace_id, document_id),),
    ).rowcount
    counts["ai_task_runs"] = conn.execute(
        """
        UPDATE ai_task_runs
        SET command = command - 'input_markdown' - 'input_blocks',
            result = CASE WHEN result IS NULL THEN NULL ELSE %(scrubbed)s END
        WHERE workspace_id = %(ws)s AND command->>'document_id' = %(doc)s AND kind = ANY(%(kinds)s)
        """,
        {**params, "scrubbed": _SCRUBBED, "kinds": _DOCUMENT_TASK_KINDS},
    ).rowcount
    counts["agent_turn_results"] = conn.execute(
        """
        UPDATE agent_runs SET result = result - 'edit' - 'generated_markdown'
        WHERE workspace_id = %(ws)s AND document_id = %(doc)s AND action = 'markdown_turn' AND result IS NOT NULL
        """,
        params,
    ).rowcount
    counts["pipeline_runs"] = conn.execute(
        """
        UPDATE pipeline_runs SET manifest = %(scrubbed)s, updated_at = now()
        WHERE workspace_id = %(ws)s AND (document_id = %(doc)s OR id::text = ANY(%(runs)s)) AND manifest IS NOT NULL
        """,
        {**params, "scrubbed": _SCRUBBED},
    ).rowcount
    # 휴지통 이동 때 지웠어야 하는 행이다. 그 command가 처리되지 못했어도 본문이 남지 않게 다시 지운다.
    for table in ("source_blocks", "source_block_snapshots", "document_quality_reviews", "document_derived_state"):
        counts[table] = conn.execute(f"DELETE FROM {table} WHERE document_id = %(doc)s", params).rowcount
    counts["evidence"], counts["evidence_pending"] = _blank_evidence(conn, params)
    counts["ai_task_changes"] = _delete_journal_changes(conn, params)
    blanked, pending = _blank_journal_evidence(conn, params)
    counts["journal_evidence"] = blanked
    counts["evidence_pending"] += pending
    return counts


def _require_same_workspace(conn: Any, params: dict[str, str]) -> None:
    """source_blocks처럼 workspace_id가 없는 표를 지우기 전에 이 문서가 command의 워크스페이스 소속인지 본다."""
    if conn.execute(
        """
        SELECT 1 FROM pipeline_runs WHERE document_id = %(doc)s AND workspace_id <> %(ws)s
        UNION ALL
        SELECT 1 FROM document_wiki_links WHERE document_id = %(doc)s AND workspace_id <> %(ws)s
        LIMIT 1
        """,
        params,
    ).fetchone():
        raise ValueError("document purge does not match the document workspace")


def _target_operations(conn: Any, params: dict[str, str]) -> list[dict[str, Any]]:
    """이 문서를 대상으로 한 작업. 새로 만든 문서는 계획에 id가 없어 실행 결과와 스냅샷으로 찾는다."""
    return conn.execute(
        """
        SELECT operation.id, plan.run_id, operation.arguments->>'content_artifact_id' AS artifact_id
        FROM agent_plan_operations operation
        JOIN agent_plans plan ON plan.id = operation.plan_id
        JOIN agent_runs run ON run.id = plan.run_id
        WHERE run.workspace_id = %(ws)s
          AND (
              operation.target_id = %(doc)s
              OR operation.arguments->>'document_id' = %(doc)s
              OR EXISTS (
                  SELECT 1 FROM agent_tool_executions execution
                  WHERE execution.operation_id = operation.id
                    AND %(doc)s IN (
                        execution.response_metadata->>'id',
                        execution.response_metadata->>'document_id',
                        execution.response_metadata#>>'{before,id}',
                        execution.response_metadata#>>'{arguments,document_id}'
                    )
              )
          )
        ORDER BY operation.id
        """,
        params,
    ).fetchall()


def _scrub_agent_operations(conn: Any, params: dict[str, str]) -> dict[str, int]:
    operations = _target_operations(conn, params)
    operation_ids = [row["id"] for row in operations]
    run_ids = sorted({row["run_id"] for row in operations})
    # 승인·취소는 run 행을 FOR UPDATE로 잠근다. 지우는 동안 겹치지 않게 하고, checkpoint 트리거의 KEY SHARE는 막지 않는다.
    conn.execute("SELECT id FROM agent_runs WHERE id = ANY(%s) ORDER BY id FOR NO KEY UPDATE", (run_ids,))
    undo = conn.execute(
        """
        UPDATE agent_tool_executions SET response_metadata = '{"document_purged": true}'::jsonb
        WHERE operation_id = ANY(%s) AND idempotency_key LIKE 'undo:%%'
        """,
        (operation_ids,),
    ).rowcount
    # 실행 결과는 후속 작업의 참조와 대상 판별에 쓰는 식별자·버전만 남긴다.
    forward = conn.execute(
        """
        UPDATE agent_tool_executions
        SET response_metadata = jsonb_strip_nulls(jsonb_build_object(
            'id', response_metadata->'id',
            'document_id', response_metadata->'document_id',
            'current_version', response_metadata->'current_version',
            'document_purged', true))
        WHERE operation_id = ANY(%s) AND idempotency_key NOT LIKE 'undo:%%'
        """,
        (operation_ids,),
    ).rowcount
    conn.execute(
        "UPDATE agent_plan_operations SET reason = %s, updated_at = now() WHERE id = ANY(%s) AND tool_name = ANY(%s)",
        (_PURGED_REASON, operation_ids, _CONTENT_TOOLS),
    )
    skipped = conn.execute(
        """
        UPDATE agent_plan_operations SET status = 'skipped', error_code = %s, updated_at = now()
        WHERE id = ANY(%s) AND status = 'pending'
        """,
        (DOCUMENT_PURGED_ERROR_CODE, operation_ids),
    ).rowcount
    artifact_ids = [row["artifact_id"] for row in operations if row["artifact_id"]]
    conn.execute(
        """
        UPDATE agent_run_artifacts SET display_name = NULL, summary = NULL
        WHERE workspace_id = %s AND (document_id = %s OR id = ANY(%s))
        """,
        (params["ws"], params["doc"], artifact_ids),
    )
    return {"undo_snapshots": undo, "tool_results": forward, "skipped_operations": skipped}


def _blank_evidence(conn: Any, params: dict[str, str]) -> tuple[int, int]:
    changed = pending = 0
    for target in _EVIDENCE_COLUMNS:
        table, column, key = target
        rows = conn.execute(
            f"SELECT {key} AS key, {column}::text AS value FROM {table} "
            f"WHERE workspace_id = %s AND {column}::text LIKE %s",
            (params["ws"], f"%{params['doc']}%"),
        ).fetchall()
        for row in rows:
            blanked = _blank_row(conn, target, row["key"], row["value"], params["doc"])
            changed += blanked is True
            pending += blanked is None
    return changed, pending


def _workspace_run_ids(conn: Any, workspace_id: str) -> list[str]:
    """작업 기록은 워크스페이스 열이 없다. 조인하면 계획기가 표 전체의 본문을 먼저 훑을 수 있어 run id를 먼저 읽어 인덱스로 찾는다."""
    return [row["id"] for row in conn.execute("SELECT id FROM ai_task_runs WHERE workspace_id = %s", (workspace_id,)).fetchall()]


def _blank_journal_evidence(conn: Any, params: dict[str, Any]) -> tuple[int, int]:
    """다른 문서 run의 작업 기록에 든 이 문서의 근거를 비운다. 지우면 그 run을 취소할 때 다른 문서의 행이 되돌아가지 않는다."""
    changed = pending = 0
    rows = conn.execute(
        """
        SELECT change.id, change.before_value::text AS before_value, change.after_value::text AS after_value
        FROM ai_task_changes change
        WHERE change.run_id = ANY(%(runs)s) AND change.table_name = ANY(%(tables)s)
          AND (change.before_value::text LIKE %(pattern)s OR change.after_value::text LIKE %(pattern)s)
        """,
        {"runs": _workspace_run_ids(conn, params["ws"]), "tables": _EVIDENCE_JOURNAL_TABLES, "pattern": f"%{params['doc']}%"},
    ).fetchall()
    for row in rows:
        for column in ("before_value", "after_value"):
            if row[column] is None:
                continue
            blanked = _blank_row(conn, ("ai_task_changes", column, "id"), row["id"], row[column], params["doc"])
            changed += blanked is True
            pending += blanked is None
    return changed, pending


def _blank_row(conn: Any, target: tuple[str, str, str], row_key: Any, stored: str, document_id: str) -> bool | None:
    """읽은 값이 그대로일 때만 쓴다. 근거는 임의의 깊이에 있어 jsonb 경로로 지정할 수 없고, 진행 중 run이 같은 행을 갱신한다.

    float로 옮기면 달라지는 숫자가 있어 jsonb가 아니라 text로 비교한다. 비웠으면 True, 비울 것이 없으면 False, 포기하면 None이다.
    """
    table, column, key = target
    for _ in range(_EVIDENCE_UPDATE_ATTEMPTS):
        value = json.loads(stored)
        cleaned = _blank_source_text(value, document_id)
        if cleaned == value:
            return False
        if conn.execute(
            f"UPDATE {table} SET {column} = %s WHERE {key} = %s AND {column}::text = %s",
            (Json(cleaned), row_key, stored),
        ).rowcount:
            return True
        row = conn.execute(
            f"SELECT {column}::text AS value FROM {table} WHERE {key} = %s AND {column} IS NOT NULL",
            (row_key,),
        ).fetchone()
        if row is None:
            return False
        stored = row["value"]
    return None


def _blank_source_text(value: Any, document_id: str) -> Any:
    """source_document_id가 이 문서인 근거의 본문만 비운다. 답변과 다른 문서의 근거는 그대로다."""
    if isinstance(value, list):
        return [_blank_source_text(item, document_id) for item in value]
    if not isinstance(value, dict):
        return value
    own = value.get("source_document_id") == document_id
    return {
        key: _EVIDENCE_TEXT_KEYS[key] if own and key in _EVIDENCE_TEXT_KEYS else _blank_source_text(item, document_id)
        for key, item in value.items()
    }


def _delete_journal_changes(conn: Any, params: dict[str, str]) -> int:
    """작업 저널의 변경 전후 값에 든 본문을 지운다. 행을 비우지 않고 지우는 이유는 비운 값을 되돌리기가 그대로 쓰기 때문이다.

    지운 행은 작업 취소가 되돌리지 않으므로 본문이 되살아나지 않는다. 개념 페이지는 여러 문서의 파생이라 남긴다.
    """
    query = {
        **params,
        "pages": _source_page_ids(conn, params["ws"], params["doc"]),
        "source_key": f"wiki/{params['ws']}/sources/{params['doc']}.md",
        "workspace_runs": _workspace_run_ids(conn, params["ws"]),
    }
    deleted = conn.execute(
        """
        DELETE FROM ai_task_changes vector
        WHERE vector.run_id = ANY(%(workspace_runs)s)
          AND vector.table_name = 'wiki_embedding_vectors'
          AND vector.row_key->>'id' IN (
              SELECT COALESCE(unit.after_value, unit.before_value)->>'embedding_vector_id'
              FROM ai_task_changes unit
              WHERE unit.run_id = ANY(%(workspace_runs)s) AND unit.table_name = 'wiki_embedding_units'
                AND COALESCE(unit.after_value, unit.before_value)->>'source_document_id' = %(doc)s
          )
        """,
        query,
    ).rowcount
    deleted += conn.execute(
        """
        DELETE FROM ai_task_changes change
        WHERE change.run_id = ANY(%(workspace_runs)s)
          AND (
              change.row_key->>'document_id' = %(doc)s
              OR change.before_value->>'document_id' = %(doc)s
              OR change.after_value->>'document_id' = %(doc)s
              OR change.before_value->>'source_document_id' = %(doc)s
              OR change.after_value->>'source_document_id' = %(doc)s
              OR (change.table_name = 'wiki_pages' AND change.row_key->>'id' = ANY(%(pages)s))
              OR change.row_key->>'page_id' = ANY(%(pages)s)
              OR (change.table_name = 'pipeline_runs' AND change.row_key->>'id' = ANY(%(runs)s))
              OR (change.table_name = '__object__' AND change.row_key->>'key' = %(source_key)s)
              OR (change.table_name = '__object__'
                  AND split_part(change.row_key->>'key', '/', 1) = 'wiki'
                  AND split_part(change.row_key->>'key', '/', 2) = %(ws)s
                  AND split_part(change.row_key->>'key', '/', 3) = 'pages'
                  AND split_part(change.row_key->>'key', '/', 4) = ANY(%(pages)s))
          )
        """,
        query,
    ).rowcount
    return deleted


def _delete_artifacts(workspace_id: str, document_id: str) -> DocumentCopyPurge:
    params = {"ws": workspace_id, "doc": document_id}
    with database.connect_ai() as conn:
        artifact_ids = [row["artifact_id"] for row in _target_operations(conn, params) if row["artifact_id"]]
        rows = conn.execute(
            """
            SELECT id, run_id, object_key, preview_object_key FROM agent_run_artifacts
            WHERE workspace_id = %s AND (document_id = %s OR id = ANY(%s))
            ORDER BY id
            FOR UPDATE
            """,
            (workspace_id, document_id, artifact_ids),
        ).fetchall()
        removed: list[str] = []
        failures = 0
        for row in rows:
            keys = [key for key in (row["object_key"], row["preview_object_key"]) if key]
            if not all(_is_in_run_scope(key, row["run_id"]) for key in keys):
                failures += 1
                logger.error("run 범위를 벗어난 artifact 키라 정리하지 않습니다: artifact_id=%s", row["id"])
                continue
            try:
                for key in keys:
                    _delete_if_present(key)
            except Exception as exc:
                failures += 1
                logger.warning("문서 복사본 객체 삭제 실패: artifact_id=%s type=%s", row["id"], type(exc).__name__)
                if failures >= MAX_STORAGE_FAILURES_PER_CYCLE:
                    break
                continue
            removed.append(row["id"])
        if removed:
            conn.execute("DELETE FROM agent_run_artifacts WHERE id = ANY(%s)", (removed,))
    return DocumentCopyPurge(deleted_artifacts=len(removed), pending_artifacts=len(rows) - len(removed))
