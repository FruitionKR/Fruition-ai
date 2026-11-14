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


def _pending(**overrides):
    return {
        "page_id": "page", "operation_id": "op", "document_id": "doc",
        "object_key": "key", "publication_id": "publication", "active": True,
        "sequence_revision": None, "linked_by_operation_id": None,
        "receiver_disposition": None, "receiver_rejection_reason": None, "trashed": False,
        **overrides,
    }


def _remote(**overrides):
    return {
        "page_id": "page", "ingest_operation_id": "op", "source_document_id": "doc",
        "object_key": "key", "active": True, "sequence_revision": 1,
        "lint_operation_id": None, "source_document_deleted": False, **overrides,
    }


def _reconcile(pending, remote):
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = [pending]
    return with_pending_contributions(conn, [remote], ["page"])


def test_trash_then_restore_reconciles_before_delete_event():
    # 휴지통 이동이 기여를 끄고 바로 복구됐다. 문서는 살아 있고 AI 삭제 이벤트는 아직 오지 않았다.
    remote = _remote(active=False)
    with pytest.raises(PendingContributionConflict):
        _reconcile(_pending(), remote)  # 복원 등 다른 이유로 꺼진 기여는 여전히 충돌이다.
    remote["deactivated_by_source_deletion"] = True
    assert _reconcile(_pending(), remote) == [remote]
    # 문서 없는 기록에는 이 근거를 쓰지 않는다.
    with pytest.raises(PendingContributionConflict):
        _reconcile(_pending(document_id=None), {**remote, "source_document_id": None})


RESTORE = {"publication_kind": "RESTORE", "sequence_revision": 1, "active": False}


@pytest.mark.parametrize("pending_overrides, remote_overrides, ok", [
    # 배포 전 복원 대기 행: 순번 고정, linked_by는 예전 API 응답을 옮긴 null.
    (RESTORE, {"active": False, "lint_operation_id": "lint-move"}, True),
    # 복원이 아닌 게시의 같은 모양은 그대로 충돌이다.
    ({**RESTORE, "publication_kind": "LINT"}, {"active": False, "lint_operation_id": "lint-move"}, False),
    # 그 뒤 다시 이어져 순번이 바뀌었으면 같은 행이 아니다.
    (RESTORE, {"active": False, "lint_operation_id": "lint-move",
                                                 "sequence_revision": 3}, False),
    # 순번을 모르는 대기 행(편입·lint)의 null은 "이은 기록이 아님"이다.
    ({}, {"lint_operation_id": "lint-move"}, False),
    # 값이 있는데 다르면 복원 행이어도 충돌이다.
    ({**RESTORE, "linked_by_operation_id": "lint-a"}, {"active": False, "lint_operation_id": "lint-b"}, False),
])
def test_pinned_restore_row_tolerates_only_missing_legacy_lint_id(pending_overrides, remote_overrides, ok):
    pending, remote = _pending(**pending_overrides), _remote(**remote_overrides)
    if ok:
        assert _reconcile(pending, remote) == [remote]
    else:
        with pytest.raises(PendingContributionConflict):
            _reconcile(pending, remote)
