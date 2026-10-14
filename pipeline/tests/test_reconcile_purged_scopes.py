from datetime import datetime, timezone

import pytest

import reconcile_purged_scopes as reconcile

EXPORTED_AT = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
OLD = datetime(2026, 9, 1, tzinfo=timezone.utc)
NEW = datetime(2026, 10, 11, 11, 30, tzinfo=timezone.utc)  # export 1시간 안쪽


class FakeConn:
    """질의에 따라 ID별 가장 이른 시각을 돌려주는 가짜 연결."""

    def __init__(self, workspaces, users):
        self._workspaces, self._users = workspaces, users

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query):
        rows = self._workspaces if "wiki_pages" in query else self._users
        return type("Result", (), {"fetchall": lambda _: [{"id": i, "seen": t} for i, t in rows.items()]})()


def run(apply, calls):
    return reconcile.reconcile(
        {"w1"}, {"u1"}, EXPORTED_AT, apply=apply,
        connect=lambda: FakeConn({"w1": OLD, "w2": OLD, "w3": NEW, "w4": None}, {"u1": OLD, "u2": OLD, "u3": NEW}),
        purge_workspace=lambda i: calls.append(("workspace", i)) or {"wiki_pages": 1},
        purge_user=lambda i: calls.append(("user", i)) or {"agent_runs": 1},
    )


def test_dry_run_lists_candidates_and_skips_without_purging() -> None:
    calls = []
    result = run(False, calls)
    assert result["to_purge_workspaces"] == {"w2": OLD.isoformat()}
    assert result["to_purge_users"] == {"u2": OLD.isoformat()}
    assert result["skipped_workspaces"] == {
        "w3": "created after export, skipped", "w4": "no timestamp evidence, skipped"}
    assert result["skipped_users"] == {"u3": "created after export, skipped"}
    assert result["dry_run"] is True and calls == []


def test_apply_purges_only_old_ids_missing_from_access() -> None:
    calls = []
    result = run(True, calls)
    assert calls == [("workspace", "w2"), ("user", "u2")]
    assert result["purged"]["users"] == {"u2": {"agent_runs": 1}}


def test_exported_at_requires_timezone() -> None:
    assert reconcile.parse_exported_at("2026-10-11T12:00:00+09:00").utcoffset() is not None
    with pytest.raises(Exception):
        reconcile.parse_exported_at("2026-10-11T12:00:00")


def test_empty_export_is_refused(tmp_path) -> None:
    empty = tmp_path / "ids.txt"
    empty.write_text("\n  \n")
    with pytest.raises(SystemExit):
        reconcile.read_ids(str(empty))
    empty.write_text("a\n\nb \n")
    assert reconcile.read_ids(str(empty)) == {"a", "b"}
