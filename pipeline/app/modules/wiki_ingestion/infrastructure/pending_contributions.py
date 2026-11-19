"""원장 반영 대기 기여.

편입은 AI 커밋 뒤 결과 이벤트를 document-svc가 적용할 때 원장(`wiki_page_contributions`)에 오른다. 그 사이 같은 페이지를
원장으로 다시 그리는 작업은 이 편입을 모른다. 편입이 커밋할 때 대기 행을 남기고, 원장을 읽을 때 원장에 아직 없는 대기 기여를
뒤에 붙인다. document-svc도 반영 순서대로 순번을 주므로 뒤에 붙이는 것이 반영 뒤 순서와 같다.
"""
from __future__ import annotations

from typing import Any

import psycopg


class PendingContributionConflict(RuntimeError):
    """대기 기여가 원장 행과 어긋나거나 수신 측이 게시를 거절했다. 기다려도 풀리지 않는다."""


class PendingContributionUnresolved(RuntimeError):
    """대기 기여가 아직 원장에 오르지 않았고 순번도 없다. document가 게시를 반영하면 풀린다."""


def record_pending_contributions(
    conn: psycopg.Connection,
    workspace_id: str,
    operation_id: str,
    document_id: str,
    artifacts: list[dict[str, Any]],
    publication_id: str | None = None,
) -> None:
    """이번 편입이 기여 JSON을 남긴 개념 페이지를 대기 행으로 남긴다. 편입 커밋과 같은 트랜잭션에서 부른다."""
    rows = [
        {
            "page_id": str(item["page_id"]),
            "operation_id": operation_id,
            "document_id": document_id,
            "object_key": str(item["contribution_key"]),
            "active": True,
        }
        for item in artifacts
        if item.get("contribution_key")
    ]
    record_pending_deltas(conn, workspace_id, rows, publication_id=publication_id)


def record_pending_deltas(
    conn: psycopg.Connection,
    workspace_id: str,
    rows: list[dict[str, Any]],
    *,
    publication_id: str | None = None,
) -> None:
    """Persist exact desired Document ledger rows in the publication transaction."""
    for item in rows:
        transition = publication_id or "legacy"
        conn.execute(
            """
            UPDATE wiki_pending_contributions
            SET superseded_at = clock_timestamp()
            WHERE page_id = %s AND operation_id = %s
              AND publication_id <> %s AND superseded_at IS NULL
            """,
            (str(item["page_id"]), str(item["operation_id"]), transition),
        )
        values = (
            str(item["page_id"]),
            str(item["operation_id"]),
            workspace_id,
            item.get("document_id"),
            str(item["object_key"]),
            bool(item.get("active", True)),
            item.get("sequence_revision"),
            item.get("linked_by_operation_id"),
            transition,
        )
        stored = conn.execute(
            """
            INSERT INTO wiki_pending_contributions (
                page_id, operation_id, workspace_id, document_id, object_key,
                active, sequence_revision, linked_by_operation_id, publication_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (page_id, operation_id, publication_id) DO NOTHING
            RETURNING page_id
            """,
            values,
        ).fetchone()
        if stored is None:
            existing = conn.execute(
                """
                SELECT workspace_id, document_id, object_key, active, sequence_revision,
                       linked_by_operation_id, publication_id
                FROM wiki_pending_contributions
                WHERE page_id = %s AND operation_id = %s AND publication_id = %s
                """,
                (values[0], values[1], transition),
            ).fetchone()
            expected = {
                "workspace_id": workspace_id,
                "document_id": item.get("document_id"),
                "object_key": str(item["object_key"]),
                "active": bool(item.get("active", True)),
                "sequence_revision": item.get("sequence_revision"),
                "linked_by_operation_id": item.get("linked_by_operation_id"),
                "publication_id": transition,
            }
            if existing is None or dict(existing) != expected:
                raise RuntimeError(
                    f"pending contribution conflicts: {item['page_id']}:{item['operation_id']}"
                )


def with_pending_contributions(
    conn: psycopg.Connection,
    ledger: list[dict[str, Any]],
    page_ids: list[str],
    *,
    cleanup_resolved: bool = True,
) -> list[dict[str, Any]]:
    """원장 행 뒤에 원장에 아직 없는 대기 기여를 활성 행으로 붙인다. 원장에 오른 대기 행은 지운다."""
    # ponytail: 원장에 오른 행은 그 페이지를 다시 읽을 때만 지운다. 다시 읽히지 않는 페이지의 행은 남지만 결과에는 영향이 없다.
    # 쌓이는 양이 문제 되면 lint가 워크스페이스 단위로 정리한다.
    if not page_ids:
        return ledger
    pending = conn.execute(
        """
        SELECT pending.page_id, pending.operation_id, pending.document_id, pending.object_key,
               pending.publication_id, publication.kind AS publication_kind,
               pending.active, pending.sequence_revision, pending.linked_by_operation_id,
               outbox.receiver_disposition, outbox.receiver_rejection_reason,
               EXISTS (
                   SELECT 1 FROM wiki_source_tombstones tombstone
                   WHERE tombstone.workspace_id = pending.workspace_id
                     AND tombstone.document_id = pending.document_id
                     AND tombstone.deleted_at > pending.created_at
               ) AS trashed
        FROM wiki_pending_contributions pending
        LEFT JOIN wiki_publication_outbox outbox USING (publication_id)
        LEFT JOIN wiki_publications publication USING (publication_id)
        WHERE pending.page_id = ANY(%s) AND pending.superseded_at IS NULL
        ORDER BY pending.created_at, pending.operation_id
        """,
        (list(page_ids),),
    ).fetchall()
    if not pending:
        return ledger
    remote = {
        (str(item.get("page_id")), str(item.get("ingest_operation_id"))): item
        for item in ledger
    }
    resolved = []
    for row in pending:
        key = (str(row["page_id"]), str(row["operation_id"]))
        if row["receiver_disposition"] == "REJECTED_CONFLICT":
            raise PendingContributionConflict(
                f"publication was rejected by receiver: {key[0]}:{key[1]}:"
                f"{row['receiver_rejection_reason']}"
            )
        actual = remote.get(key)
        if actual is None:
            continue
        actual_sequence = actual.get("sequence_revision")
        if (
            not isinstance(actual_sequence, int)
            or isinstance(actual_sequence, bool)
            or actual_sequence <= 0
        ):
            raise RuntimeError(
                f"remote contribution has invalid sequence: {key[0]}:{key[1]}"
            )
        # 문서 서비스의 휴지통 반영이 AI 삭제 이벤트보다 먼저 도착할 수 있다. 휴지통에서 바로 복구하면 문서는
        # 살아 있어도 휴지통 이동이 끈 기여는 꺼진 채라, 그 끈 이유(deactivated_by_source_deletion)로 대조한다.
        source_removed = row["document_id"] and (
            actual.get("source_document_deleted") is True
            or actual.get("deactivated_by_source_deletion") is True
        )
        # 복원 대기 행의 linked_by는 순번과 함께 document 응답을 옮긴 값이다. 연결 lint ID는 행을 만들거나
        # 다시 이을 때만 바뀌고 그때 순번도 새로 받으므로, 순번이 같으면 같은 값이다. 예전 API는 이 필드가 없어
        # 배포 전에 만든 복원 대기 행에는 null로 남았다.
        lint_matches = actual.get("lint_operation_id") == row["linked_by_operation_id"] or (
            row.get("publication_kind") == "RESTORE"
            and row["linked_by_operation_id"] is None and row["sequence_revision"] is not None
        )
        exact = (
            actual.get("source_document_id") == row["document_id"]
            and bool(actual.get("active")) == bool(row["active"] and not row["trashed"] and not source_removed)
            and str(actual.get("object_key") or "") == str(row["object_key"])
            and lint_matches
            and (
                row["sequence_revision"] is None
                or actual_sequence == int(row["sequence_revision"])
            )
        )
        if not exact:
            raise PendingContributionConflict(f"remote contribution contradicts pending state: {key[0]}:{key[1]}")
        resolved.append(row)
    if cleanup_resolved:
        for row in resolved:
            conn.execute(
                "DELETE FROM wiki_pending_contributions WHERE page_id = %s AND operation_id = %s AND publication_id = %s",
                (row["page_id"], row["operation_id"], row["publication_id"]),
            )
    merged = list(ledger)
    for row in pending:
        page_id = str(row["page_id"])
        if (page_id, str(row["operation_id"])) in remote:
            continue
        local_sequence = row["sequence_revision"]
        if local_sequence is None:
            raise PendingContributionUnresolved(
                f"pending contribution sequence is unresolved: {page_id}:{row['operation_id']}"
            )
        merged.append({
            "page_id": page_id,
            "ingest_operation_id": str(row["operation_id"]),
            "source_document_id": row["document_id"],
            "sequence_revision": int(local_sequence),
            "active": bool(row["active"] and not row["trashed"]),
            "object_key": row["object_key"],
            "lint_operation_id": row["linked_by_operation_id"],
        })
    return merged
