import pytest

import reconcile_purged_scopes as reconcile


class FakeConn:
    """UNION 질의 하나가 한 종류의 ID 집합을 돌려주는 가짜 연결."""

    def __init__(self, workspaces, users):
        self._workspaces, self._users = workspaces, users

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query):
        ids = self._workspaces if "wiki_pages" in query else self._users
        return type("Result", (), {"fetchall": lambda _: [{"id": i} for i in ids]})()


def run(apply, calls):
    return reconcile.reconcile(
        {"w1"}, {"u1"}, apply=apply,
        connect=lambda: FakeConn({"w1", "w2", "w3"}, {"u1", "u2"}),
        purge_workspace=lambda i: calls.append(("workspace", i)) or {"wiki_pages": 1},
        purge_user=lambda i: calls.append(("user", i)) or {"agent_runs": 1},
    )


def test_dry_run_lists_stale_ids_without_purging() -> None:
    calls = []
    result = run(False, calls)
    assert result["stale_workspace_ids"] == ["w2", "w3"] and result["stale_user_ids"] == ["u2"]
    assert result["dry_run"] is True and calls == []


def test_apply_purges_only_ids_missing_from_access() -> None:
    calls = []
    result = run(True, calls)
    assert calls == [("workspace", "w2"), ("workspace", "w3"), ("user", "u2")]
    assert result["purged"]["users"] == {"u2": {"agent_runs": 1}}


def test_empty_export_is_refused(tmp_path) -> None:
    empty = tmp_path / "ids.txt"
    empty.write_text("\n  \n")
    with pytest.raises(SystemExit):
        reconcile.read_ids(str(empty))
    empty.write_text("a\n\nb \n")
    assert reconcile.read_ids(str(empty)) == {"a", "b"}
