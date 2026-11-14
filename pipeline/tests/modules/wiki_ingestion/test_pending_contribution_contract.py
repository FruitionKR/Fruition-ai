from unittest.mock import Mock

import pytest

from app.modules.wiki_ingestion.infrastructure.pending_contributions import (
    PendingContributionConflict,
    with_pending_contributions,
)


@pytest.mark.parametrize("linked_by", [None, "lint-move"])
def test_document_response_reconciles_lint_rows(linked_by):
    pending = {
        "page_id": "page", "operation_id": "op", "document_id": None,
        "object_key": "wiki/ws/pages/page/ops/op.json", "publication_id": "wiki:lint",
        "active": True, "sequence_revision": None, "linked_by_operation_id": linked_by,
        "receiver_disposition": None, "receiver_rejection_reason": None, "trashed": False,
    }
    remote = {
        "page_id": "page", "ingest_operation_id": "op", "source_document_id": None,
        "object_key": pending["object_key"], "active": True, "sequence_revision": 2,
        "lint_operation_id": linked_by,
    }
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = [pending]

    if linked_by:
        # 이전 document API는 이 필드를 빠뜨려 정상 반영된 이동·분리 기록도 다음 lint에서 거절했다.
        missing = {key: value for key, value in remote.items() if key != "lint_operation_id"}
        with pytest.raises(PendingContributionConflict, match="contradicts pending state"):
            with_pending_contributions(conn, [missing], ["page"])

    assert with_pending_contributions(conn, [remote], ["page"]) == [remote]
    assert any("DELETE FROM wiki_pending_contributions" in call.args[0]
               for call in conn.execute.call_args_list)
    conn.execute.return_value.fetchall.return_value = []
    assert with_pending_contributions(conn, [remote], ["page"]) == [remote]


@pytest.mark.parametrize("delete_event_received", [False, True])
def test_confirmed_trash_reconciles_before_delete_event(delete_event_received):
    pending = {
        "page_id": "page", "operation_id": "op", "document_id": "doc",
        "object_key": "wiki/ws/pages/page/ops/op.json", "publication_id": "wiki:op",
        "active": True, "sequence_revision": 1, "linked_by_operation_id": None,
        "receiver_disposition": None, "receiver_rejection_reason": None, "trashed": False,
    }
    remote = {
        "page_id": "page", "ingest_operation_id": "op", "source_document_id": "doc",
        "object_key": pending["object_key"], "active": False, "sequence_revision": 1,
        "lint_operation_id": None,
    }
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = [pending]
    with pytest.raises(PendingContributionConflict, match="contradicts pending state"):
        with_pending_contributions(conn, [remote], ["page"])
    remote["source_document_deleted"] = True
    pending["trashed"] = delete_event_received
    assert with_pending_contributions(conn, [remote], ["page"]) == [remote]
    assert any("DELETE FROM wiki_pending_contributions" in call.args[0]
               for call in conn.execute.call_args_list)

    conn.execute.return_value.fetchall.return_value = []
    assert with_pending_contributions(conn, [remote], ["page"]) == [remote]


@pytest.mark.parametrize("mismatch", [
    {"source_document_id": "other"}, {"object_key": "wrong"},
    {"lint_operation_id": "other"}, {"sequence_revision": 2}, {"active": True},
])
def test_confirmed_trash_does_not_hide_other_conflicts(mismatch):
    pending = {
        "page_id": "page", "operation_id": "op", "document_id": "doc",
        "object_key": "key", "publication_id": "publication", "active": True,
        "sequence_revision": 1, "linked_by_operation_id": None,
        "receiver_disposition": None, "receiver_rejection_reason": None, "trashed": False,
    }
    remote = {
        "page_id": "page", "ingest_operation_id": "op", "source_document_id": "doc",
        "object_key": "key", "active": False, "sequence_revision": 1,
        "lint_operation_id": None, "source_document_deleted": True, **mismatch,
    }
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = [pending]
    with pytest.raises(PendingContributionConflict):
        with_pending_contributions(conn, [remote], ["page"])
