import json
from contextlib import nullcontext

import pytest

from app.modules.wiki_ingestion.application.models import WikiMaintenanceCommand as _WikiMaintenanceCommand
from app.modules.wiki_ingestion.domain.orphan_link_lint import (
    find_orphan_links,
)
from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as repository,
)
from app.modules.wiki_ingestion.infrastructure import wiki_maintenance


def WikiMaintenanceCommand(**data: object) -> _WikiMaintenanceCommand:
    data.setdefault("provider", "openai")
    data.setdefault("model", "gpt-5-nano")
    return _WikiMaintenanceCommand(**data)


def test_lint_keeps_link_supported_by_any_active_contribution() -> None:
    shared = {
        "source": "concept:shared",
        "target": "concept:target",
        "relation": "related_to",
    }
    unsupported = {
        "source": "concept:shared",
        "target": "concept:removed",
        "relation": "related_to",
    }

    orphan_links = find_orphan_links(
        current_links=[shared, unsupported],
        active_contribution_json=[
            {
                "operation_id": "op-B",
                "page_id": "shared",
                "links": [shared],
            }
        ],
        managed_contribution_json=[{"links": [shared, unsupported]}],
        deleted_page_refs=set(),
    )

    assert orphan_links == [
        {
            **unsupported,
            "reason": "no_active_support",
        }
    ]


def test_lint_marks_incident_link_when_page_is_deleted() -> None:
    link = {
        "source": "concept:deleted",
        "target": "concept:target",
        "relation": "related_to",
    }

    orphan_links = find_orphan_links(
        current_links=[link],
        active_contribution_json=[{"links": [link]}],
        managed_contribution_json=[{"links": [link]}],
        deleted_page_refs={"concept:deleted"},
    )

    assert orphan_links == [{**link, "reason": "endpoint_deleted"}]


def test_lint_preserves_link_that_has_never_been_managed_by_contribution_log() -> None:
    legacy_link = {
        "source": "concept:legacy",
        "target": "concept:target",
        "relation": "related_to",
    }

    orphan_links = find_orphan_links(
        current_links=[legacy_link],
        active_contribution_json=[],
        managed_contribution_json=[],
        deleted_page_refs=set(),
    )

    assert orphan_links == []


def test_lint_replays_link_add_and_remove_actions_in_operation_order() -> None:
    link = {
        "source": "concept:shared",
        "target": "concept:target",
        "relation": "related_to",
    }

    removed = find_orphan_links(
        current_links=[link],
        active_contribution_json=[
            {"artifact_type": "ingest", "links": [link]},
            {"artifact_type": "lint", "removed_links": [link]},
        ],
        managed_contribution_json=[
            {"artifact_type": "ingest", "links": [link]},
            {"artifact_type": "lint", "removed_links": [link]},
        ],
        deleted_page_refs=set(),
    )
    readded = find_orphan_links(
        current_links=[link],
        active_contribution_json=[
            {"artifact_type": "ingest", "links": [link]},
            {"artifact_type": "lint", "removed_links": [link]},
            {"artifact_type": "lint", "added_links": [link]},
        ],
        managed_contribution_json=[
            {"artifact_type": "ingest", "links": [link]},
            {"artifact_type": "lint", "removed_links": [link]},
            {"artifact_type": "lint", "added_links": [link]},
        ],
        deleted_page_refs=set(),
    )

    assert removed == [{**link, "reason": "no_active_support"}]
    assert readded == []


def test_postgres_lint_reads_active_contribution_logs_and_removes_orphan(
    monkeypatch,
) -> None:
    shared = {
        "source": "concept:shared",
        "target": "concept:target",
        "relation": "related_to",
    }
    unsupported = {
        "source": "concept:shared",
        "target": "concept:removed",
        "relation": "related_to",
    }

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class Connection:
        def execute(self, query, _params):
            normalized = " ".join(query.split())
            if "SELECT page.id" in normalized:
                return Result([{"id": "page-shared"}])
            if "FROM wiki_page_links link" in normalized:
                return Result(
                    [
                        {
                            **shared,
                            "source_status": "active",
                            "target_status": "active",
                        },
                        {
                            **unsupported,
                            "source_status": "active",
                            "target_status": "active",
                        },
                    ]
                )
            raise AssertionError(normalized)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    removed = []
    read_keys = []
    connection = Connection()

    def unexpected_connect():
        raise AssertionError("shared lint transaction을 사용해야 한다")

    monkeypatch.setattr(repository, "connect", unexpected_connect)
    monkeypatch.setattr(
        repository,
        "read_contributions",
        lambda page_ids, workspace_id: [
            {
                "active": True,
                "object_key": "wiki/ws/pages/shared/ops/B.json",
            },
            {
                "active": False,
                "object_key": "wiki/ws/pages/shared/ops/A.json",
            },
        ],
    )
    monkeypatch.setattr(
        repository,
        "read_text_object",
        lambda key: read_keys.append(key)
        or json.dumps(
            {"links": [shared if key.endswith("B.json") else unsupported]},
            ensure_ascii=False,
        ),
    )
    monkeypatch.setattr(
        repository,
        "_remove_stale_relations",
        lambda _conn, relations, _active, _user, _workspace: (
            removed.extend(relations) or relations
        ),
    )

    result = repository.lint_orphan_wiki_links(
        "user-1",
        "ws-1",
        apply=True,
        connection=connection,
    )

    assert read_keys == [
        "wiki/ws/pages/shared/ops/B.json",
        "wiki/ws/pages/shared/ops/A.json",
    ]
    assert result["orphan_link_candidates"] == [
        {**unsupported, "reason": "no_active_support"}
    ]
    assert result["removed_orphan_links"] == [
        {**unsupported, "reason": "no_active_support"}
    ]
    assert removed == [{**unsupported, "reason": "no_active_support"}]


def test_wiki_maintenance_reserves_embedding_before_commit_and_keeps_result_when_worker_start_fails(
    monkeypatch,
) -> None:
    calls = []

    class EmbeddingJob:
        def start(self, run_id: str, page_ids: list[str]) -> None:
            calls.append(("embedding", run_id, page_ids))
            raise RuntimeError("thread start failed")

    class Transaction:
        def __enter__(self):
            calls.append("begin")
            return self

        def __exit__(self, exc_type, *_args):
            calls.append("rollback" if exc_type else "commit")
            return False

    transaction = Transaction()
    monkeypatch.setattr(wiki_maintenance.database, "connect", lambda: transaction)
    monkeypatch.setattr(
        wiki_maintenance,
        "concept_write_lock",
        lambda *_args: nullcontext(),
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "lint_wiki_workspace",
        lambda *_args, **kwargs: (
            calls.append(("lint", kwargs["write_log"]))
            or {
                "workspace_id": "ws-1",
                "materialized_promotions": [{"page_id": "page-1"}],
                "merged_promotions": [
                    {"page_id": "page-2"},
                    {"page_id": "page-1"},
                ],
            }
        ),
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "lint_orphan_wiki_links",
        lambda *_args, **kwargs: (
            calls.append(("orphan", kwargs["apply"]))
            or {
                "orphan_link_candidates": [{"reason": "no_active_support"}],
                "removed_orphan_links": [{"reason": "no_active_support"}],
            }
        ),
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "write_wiki_lint_log",
        lambda result: calls.append(("log", result["removed_orphan_links"])),
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "persist_lint_operation_result",
        lambda *_args, **_kwargs: calls.append(("artifact", "lint-op-1")) or [],
        raising=False,
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "apply_lint_object_changes",
        lambda _result: calls.append("objects"),
    )
    monkeypatch.setattr(
        wiki_maintenance,
        "reserve_page_embeddings",
        lambda connection, page_ids, model: calls.append(
            ("reserve", connection is transaction, page_ids, model)
        ),
    )

    result = wiki_maintenance.PostgresWikiMaintenance(EmbeddingJob()).lint(
        WikiMaintenanceCommand(
            user_id="user-1",
            workspace_id="ws-1",
            operation_id="lint-op-1",
            dry_run=False,
        )
    )

    assert result["removed_orphan_links"] == [{"reason": "no_active_support"}]
    assert result["changed_pages"] == []
    assert calls == [
        "begin",
        ("lint", False),
        ("orphan", True),
        ("artifact", "lint-op-1"),
        ("log", [{"reason": "no_active_support"}]),
        "objects",
        ("reserve", True, ["page-1", "page-2"], "BAAI/bge-m3"),
        "commit",
        ("embedding", "lint-op-1", ["page-1", "page-2"]),
    ]


@pytest.mark.parametrize("failure_step", ["artifact", "log"])
def test_wiki_maintenance_rolls_back_when_operation_log_persistence_fails(
    monkeypatch,
    failure_step: str,
) -> None:
    calls = []

    class Transaction:
        def __enter__(self):
            calls.append("begin")
            return self

        def __exit__(self, exc_type, *_args):
            calls.append("rollback" if exc_type else "commit")
            return False

    transaction = Transaction()
    monkeypatch.setattr(wiki_maintenance.database, "connect", lambda: transaction)
    monkeypatch.setattr(
        wiki_maintenance,
        "concept_write_lock",
        lambda *_args: nullcontext(),
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "lint_wiki_workspace",
        lambda *_args, **kwargs: (
            calls.append(("lint", kwargs["connection"] is transaction))
            or {"workspace_id": "ws-1"}
        ),
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "lint_orphan_wiki_links",
        lambda *_args, **kwargs: (
            calls.append(("orphan", kwargs["connection"] is transaction))
            or {
                "orphan_link_candidates": [],
                "removed_orphan_links": [],
            }
        ),
    )

    def persist_artifact(*_args, **kwargs):
        calls.append(("artifact", kwargs["connection"] is transaction))
        if failure_step == "artifact":
            raise OSError("artifact write failed")
        return []

    def write_log(_result):
        calls.append("log")
        if failure_step == "log":
            raise OSError("lint log write failed")

    monkeypatch.setattr(
        wiki_maintenance.database,
        "persist_lint_operation_result",
        persist_artifact,
    )
    monkeypatch.setattr(wiki_maintenance.database, "write_wiki_lint_log", write_log)
    monkeypatch.setattr(
        wiki_maintenance.database,
        "apply_lint_object_changes",
        lambda _result: calls.append("objects"),
    )

    with pytest.raises(OSError):
        wiki_maintenance.PostgresWikiMaintenance().lint(
            WikiMaintenanceCommand(
                user_id="user-1",
                workspace_id="ws-1",
                operation_id="lint-op-1",
                dry_run=False,
            )
        )

    assert calls[:4] == [
        "begin",
        ("lint", True),
        ("orphan", True),
        ("artifact", True),
    ]
    assert calls[-1] == "rollback"


def test_wiki_maintenance_deletes_written_objects_when_later_step_fails(
    monkeypatch,
) -> None:
    """persist_lint_operation_result가 storage에 쓴 뒤 후속 단계(write_wiki_lint_log)가
    실패하면, DB는 롤백되고 이미 쓴 lint 산출물도 함께 지워져 orphan이 남지 않아야 한다.

    기존 rollback 테스트는 함수 전체를 모킹해 storage 효과를 볼 수 없으므로,
    실제로 어떤 key가 쓰였고 어떤 key가 지워졌는지를 직접 기록하는 fake를 사용한다.
    """
    calls = []
    deleted_keys: list[str] = []

    class Transaction:
        def __enter__(self):
            calls.append("begin")
            return self

        def __exit__(self, exc_type, *_args):
            calls.append("rollback" if exc_type else "commit")
            return False

    transaction = Transaction()
    monkeypatch.setattr(wiki_maintenance.database, "connect", lambda: transaction)
    monkeypatch.setattr(
        wiki_maintenance,
        "concept_write_lock",
        lambda *_args: nullcontext(),
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "lint_wiki_workspace",
        lambda *_args, **_kwargs: {"workspace_id": "ws-1"},
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "lint_orphan_wiki_links",
        lambda *_args, **_kwargs: {
            "orphan_link_candidates": [],
            "removed_orphan_links": [],
        },
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "persist_lint_operation_result",
        lambda *_args, **_kwargs: (
            calls.append("artifact")
            or [
                {
                    "page_id": "page-1",
                    "markdown_key": "wiki/ws-1/pages/page-1/ops/lint-op-1.md",
                    "contribution_key": "wiki/ws-1/pages/page-1/ops/lint-op-1.json",
                }
            ]
        ),
    )

    def write_log(_result):
        calls.append("log")
        raise OSError("lint log write failed")

    monkeypatch.setattr(wiki_maintenance.database, "write_wiki_lint_log", write_log)
    monkeypatch.setattr(
        wiki_maintenance.database,
        "apply_lint_object_changes",
        lambda _result: calls.append("objects") or [],
    )
    monkeypatch.setattr(
        wiki_maintenance,
        "delete_object",
        lambda key: deleted_keys.append(key),
    )

    with pytest.raises(OSError):
        wiki_maintenance.PostgresWikiMaintenance().lint(
            WikiMaintenanceCommand(
                user_id="user-1",
                workspace_id="ws-1",
                operation_id="lint-op-1",
                dry_run=False,
            )
        )

    assert calls == ["begin", "artifact", "log", "rollback"]
    assert deleted_keys == [
        "wiki/ws-1/pages/page-1/ops/lint-op-1.md",
        "wiki/ws-1/pages/page-1/ops/lint-op-1.json",
    ]


def test_wiki_maintenance_dry_run_does_not_persist_operation_artifacts(
    monkeypatch,
) -> None:
    calls = []

    class EmbeddingJob:
        def start(self, run_id: str, page_ids: list[str]) -> None:
            calls.append((run_id, page_ids))

    monkeypatch.setattr(
        wiki_maintenance.database,
        "lint_wiki_workspace",
        lambda *_args, **_kwargs: {
            "workspace_id": "ws-1",
            "materialized_promotions": [{"page_id": "page-1"}],
        },
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "lint_orphan_wiki_links",
        lambda *_args, **_kwargs: {
            "orphan_link_candidates": [],
            "removed_orphan_links": [],
        },
    )
    monkeypatch.setattr(
        wiki_maintenance.database,
        "persist_lint_operation_result",
        lambda *_args: calls.append("artifact"),
        raising=False,
    )

    wiki_maintenance.PostgresWikiMaintenance(EmbeddingJob()).lint(
        WikiMaintenanceCommand(
            user_id="user-1",
            workspace_id="ws-1",
            dry_run=True,
        )
    )

    assert calls == []
