"""원장 반영 대기 기여.

편입은 AI 커밋 뒤 결과 이벤트를 document-svc가 적용할 때 원장(`wiki_page_contributions`)에 오른다. 그 사이 같은 페이지를
원장으로 다시 그리는 작업은 이 편입을 모른다. 편입이 커밋할 때 대기 행을 남기고, 원장을 읽을 때 원장에 아직 없는 대기 기여를
뒤에 붙인다. document-svc도 반영 순서대로 순번을 주므로 뒤에 붙이는 것이 반영 뒤 순서와 같다.
"""
from __future__ import annotations

from typing import Any

import psycopg


def record_pending_contributions(
    conn: psycopg.Connection,
    workspace_id: str,
    operation_id: str,
    document_id: str,
    artifacts: list[dict[str, Any]],
) -> None:
    """이번 편입이 기여 JSON을 남긴 개념 페이지를 대기 행으로 남긴다. 편입 커밋과 같은 트랜잭션에서 부른다."""
    rows = [
        (str(item["page_id"]), operation_id, workspace_id, document_id, str(item["contribution_key"]))
        for item in artifacts
        if item.get("contribution_key")
    ]
    if not rows:
        return
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO wiki_pending_contributions (page_id, operation_id, workspace_id, document_id, object_key)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (page_id, operation_id) DO NOTHING
            """,
            rows,
        )


def with_pending_contributions(
    conn: psycopg.Connection,
    ledger: list[dict[str, Any]],
    page_ids: list[str],
) -> list[dict[str, Any]]:
    """원장 행 뒤에 원장에 아직 없는 대기 기여를 활성 행으로 붙인다. 원장에 오른 대기 행은 지운다."""
    # ponytail: 원장에 오른 행은 그 페이지를 다시 읽을 때만 지운다. 다시 읽히지 않는 페이지의 행은 남지만 결과에는 영향이 없다.
    # 쌓이는 양이 문제 되면 lint가 워크스페이스 단위로 정리한다.
    if not page_ids:
        return ledger
    pending = conn.execute(
        """
        SELECT pending.page_id, pending.operation_id, pending.document_id, pending.object_key,
               EXISTS (
                   SELECT 1 FROM wiki_source_tombstones tombstone
                   WHERE tombstone.workspace_id = pending.workspace_id
                     AND tombstone.document_id = pending.document_id
                     AND tombstone.deleted_at > pending.created_at
               ) AS trashed
        FROM wiki_pending_contributions pending
        WHERE pending.page_id = ANY(%s)
        ORDER BY pending.created_at, pending.operation_id
        """,
        (list(page_ids),),
    ).fetchall()
    if not pending:
        return ledger
    ledgered = {(str(item.get("page_id")), str(item.get("ingest_operation_id"))) for item in ledger}
    resolved = [row for row in pending if (str(row["page_id"]), str(row["operation_id"])) in ledgered]
    for row in resolved:
        conn.execute(
            "DELETE FROM wiki_pending_contributions WHERE page_id = %s AND operation_id = %s",
            (row["page_id"], row["operation_id"]),
        )
    sequence: dict[str, int] = {}
    for item in ledger:
        page_id = str(item.get("page_id"))
        sequence[page_id] = max(sequence.get(page_id, 0), int(item.get("sequence_revision") or 0))
    merged = list(ledger)
    for row in pending:
        page_id = str(row["page_id"])
        if (page_id, str(row["operation_id"])) in ledgered:
            continue
        sequence[page_id] = sequence.get(page_id, 0) + 1
        merged.append({
            "page_id": page_id,
            "ingest_operation_id": str(row["operation_id"]),
            "source_document_id": row["document_id"],
            "sequence_revision": sequence[page_id],
            # 반영 전에 휴지통으로 옮긴 문서의 기여는 document-svc도 꺼진 행으로 넣는다.
            "active": not row["trashed"],
            "object_key": row["object_key"],
        })
    return merged
