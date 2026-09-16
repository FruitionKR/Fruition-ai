from cleanup_source_related_links import cleanup_source_related_links


class _CountCursor:
    def __init__(self, count: int) -> None:
        self._count = count

    def fetchone(self) -> dict[str, int]:
        return {"matched_count": self._count}


class _DeleteCursor:
    def fetchone(self) -> None:
        return None


class _Connection:
    def __init__(self, count: int) -> None:
        self.count = count
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self,
        sql: str,
        params: tuple[str, ...],
    ) -> _CountCursor | _DeleteCursor:
        self.calls.append((" ".join(sql.split()), params))
        if sql.lstrip().startswith("SELECT"):
            return _CountCursor(self.count)
        return _DeleteCursor()


def test_cleanup_defaults_to_workspace_scoped_dry_run() -> None:
    connection = _Connection(count=3)

    result = cleanup_source_related_links(
        "user-1",
        "workspace-1",
        connect=lambda: connection,
    )

    assert result["matched_count"] == 3
    assert result["deleted_count"] == 0
    assert result["dry_run"] is True
    assert len(connection.calls) == 1
    assert connection.calls[0][1] == (
        "user-1",
        "workspace-1",
        "user-1",
        "workspace-1",
    )


def test_cleanup_deletes_only_after_explicit_apply() -> None:
    connection = _Connection(count=3)

    result = cleanup_source_related_links(
        "user-1",
        "workspace-1",
        apply=True,
        connect=lambda: connection,
    )

    assert result["deleted_count"] == 3
    assert result["dry_run"] is False
    assert len(connection.calls) == 2
    assert "DELETE FROM wiki_page_links" in connection.calls[1][0]
    assert connection.calls[1][1] == (
        "user-1",
        "workspace-1",
        "user-1",
        "workspace-1",
    )
