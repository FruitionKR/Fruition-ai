from contextlib import nullcontext
from unittest.mock import Mock, patch

import pytest

from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)


def test_touch_pipeline_run_updates_only_running_run() -> None:
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.execute.return_value.fetchone.return_value = {"id": "run-1"}

    with patch.object(database, "connect", return_value=connection):
        active = database.touch_pipeline_run("run-1")

    sql, params = connection.execute.call_args.args
    assert active is True
    assert "SET updated_at = now()" in sql
    assert "pr.status = 'running'" in sql
    assert "documents" not in sql
    assert params == ("run-1",)


def test_touch_pipeline_run_returns_false_for_inactive_target() -> None:
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.execute.return_value.fetchone.return_value = None

    with patch.object(database, "connect", return_value=connection):
        active = database.touch_pipeline_run("run-deleted")

    assert active is False


def test_finish_pipeline_run_writes_only_ai_owned_tables() -> None:
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    run_cursor = Mock()
    run_cursor.fetchone.return_value = {
        "document_id": "doc-1",
        "user_id": "user-1",
        "workspace_id": "ws-1",
    }
    active_cursor = Mock()
    active_cursor.fetchone.return_value = {"status": "running"}
    connection.execute.side_effect = [run_cursor, active_cursor, Mock()]

    with (
        patch.object(database, "connect", return_value=connection),
        patch.object(database, "concept_write_lock", return_value=nullcontext()),
        patch.object(
            database,
            "_persist_wiki_outputs",
            return_value=["page-1"],
        ) as persist_outputs,
        patch.object(database, "_stored_manifest", side_effect=lambda manifest: manifest),
        patch.object(database, "_mark_derived_state_ingested") as mark_derived,
    ):
        manifest = {"post_ingest": {"cluster_normalized": {}}}
        database.finish_pipeline_run("run-1", manifest, "sha256:expected")

    persist_outputs.assert_called_once()
    assert manifest["post_ingest"]["page_ids"] == ["page-1"]
    mark_derived.assert_called_once_with("doc-1", "sha256:expected")
    sql = " ".join(call.args[0] for call in connection.execute.call_args_list)
    assert "documents" not in sql


def test_derived_state_marks_only_the_expected_hash() -> None:
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    with patch.object(database, "connect_ai", return_value=connection):
        database._mark_derived_state_ingested("doc-1", "sha256:expected")

    sql, params = connection.execute.call_args.args
    assert "ingested_hash = %s" in sql
    assert "last_edit_hash = %s" in sql
    assert params == ("sha256:expected", "doc-1", "sha256:expected")


def test_restore_updates_current_markdown_and_links(monkeypatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "wiki-bucket")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    def execute(query, params=None):
        cursor = Mock()
        normalized = " ".join(str(query).split())
        if normalized.startswith("SELECT id, page_type, slug, user_id"):
            cursor.fetchall.return_value = [
                {
                    "id": "page-c3",
                    "page_type": "concept",
                    "slug": "c3",
                    "user_id": "user-1",
                },
                {
                    "id": "page-target",
                    "page_type": "concept",
                    "slug": "target",
                    "user_id": "user-1",
                },
            ]
        elif normalized.startswith("SELECT DISTINCT embedding_vector_id"):
            cursor.fetchall.return_value = []
        elif normalized.startswith("SELECT id FROM wiki_pages"):
            cursor.fetchone.return_value = {"id": "page-target"}
        return cursor

    connection.execute.side_effect = execute
    changed = [{
        "page_id": "page-c3",
        "markdown_key": "wiki/ws-1/pages/page-c3/ops/restore-1.md",
        "title": "Restored C3",
        "summary": "Restored summary",
        "source_document_ids": ["doc-A"],
    }, {
        "page_id": "page-target",
        "markdown_key": "wiki/ws-1/pages/page-target/ops/restore-1.md",
    }]
    links = {"removed_links": [], "restored_links": [{
        "source": "concept:c3",
        "target": "concept:target",
        "relation": "related_to",
    }]}

    with (
        patch.object(database, "connect", return_value=connection),
        patch.object(database, "invalidate_concept_index") as invalidate,
        patch.object(database, "read_text_object", return_value="# Restored C3\n"),
        patch.object(database, "_persist_embedding_units") as persist_units,
    ):
        database.apply_restored_wiki_state("ws-1", changed, links, True)

    calls = connection.execute.call_args_list
    markdown_call = next(call for call in calls if "SET markdown_uri" in str(call.args[0]))
    assert markdown_call.args[1] == (
        "s3://wiki-bucket/wiki/ws-1/pages/page-c3/ops/restore-1.md",
        "Restored C3",
        "Restored summary",
        "page-c3",
        "ws-1",
    )
    assert any("DELETE FROM document_wiki_links" in str(call.args[0]) for call in calls)
    assert any("INSERT INTO document_wiki_links" in str(call.args[0]) for call in calls)
    persist_units.assert_called_once_with(
        connection,
        "page-c3",
        "doc-A",
        "# Restored C3\n",
    )
    assert any("DELETE FROM wiki_page_links WHERE from_page_id" in str(call.args[0]) for call in calls)
    assert any("INSERT INTO wiki_page_links" in str(call.args[0]) for call in calls)
    invalidate.assert_called_once_with("user-1", "ws-1")


def test_restore_resolves_unchanged_active_link_target(monkeypatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "wiki-bucket")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    def execute(query, params=None):
        cursor = Mock()
        normalized = " ".join(str(query).split())
        if normalized.startswith("SELECT id, page_type, slug, user_id"):
            if "status = 'active'" in normalized:
                cursor.fetchall.return_value = [
                    {
                        "id": "other-user-target",
                        "page_type": "concept",
                        "slug": "target",
                        "user_id": "user-2",
                    },
                    {
                        "id": "page-target",
                        "page_type": "concept",
                        "slug": "target",
                        "user_id": "user-1",
                    },
                ]
            else:
                cursor.fetchall.return_value = [{
                    "id": "page-c3",
                    "page_type": "concept",
                    "slug": "c3",
                    "user_id": "user-1",
                }]
        elif normalized.startswith("SELECT DISTINCT embedding_vector_id"):
            cursor.fetchall.return_value = []
        return cursor

    connection.execute.side_effect = execute
    with (
        patch.object(database, "connect", return_value=connection),
        patch.object(database, "invalidate_concept_index"),
        patch.object(database, "read_text_object", return_value="# Restored C3\n"),
    ):
        database.apply_restored_wiki_state(
            "ws-1",
            [{
                "page_id": "page-c3",
                "markdown_key": "wiki/ws-1/pages/page-c3/ops/restore-1.md",
            }],
            {
                "removed_links": [],
                "restored_links": [{
                    "source": "concept:c3",
                    "target": "concept:target",
                    "relation": "related_to",
                }],
            },
            False,
        )

    link_call = next(
        call
        for call in connection.execute.call_args_list
        if "INSERT INTO wiki_page_links" in str(call.args[0])
    )
    assert link_call.args[1] == (
        "page-c3",
        "page-target",
        "related_to",
        None,
        None,
        "ws-1",
    )


def test_restore_does_not_resolve_unchanged_active_link_source(monkeypatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "wiki-bucket")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    def execute(query, params=None):
        cursor = Mock()
        normalized = " ".join(str(query).split())
        if normalized.startswith("SELECT id, page_type, slug, user_id"):
            if "status = 'active'" in normalized:
                cursor.fetchall.return_value = [{
                    "id": "page-unchanged-source",
                    "page_type": "concept",
                    "slug": "unchanged-source",
                    "user_id": "user-1",
                }]
            else:
                cursor.fetchall.return_value = [{
                    "id": "page-c3",
                    "page_type": "concept",
                    "slug": "c3",
                    "user_id": "user-1",
                }]
        elif normalized.startswith("SELECT DISTINCT embedding_vector_id"):
            cursor.fetchall.return_value = []
        return cursor

    connection.execute.side_effect = execute
    with (
        patch.object(database, "connect", return_value=connection),
        patch.object(database, "invalidate_concept_index"),
        patch.object(database, "read_text_object", return_value="# Restored C3\n"),
    ):
        database.apply_restored_wiki_state(
            "ws-1",
            [{
                "page_id": "page-c3",
                "markdown_key": "wiki/ws-1/pages/page-c3/ops/restore-1.md",
            }],
            {
                "removed_links": [],
                "restored_links": [{
                    "source": "concept:unchanged-source",
                    "target": "concept:c3",
                    "relation": "related_to",
                }],
            },
            False,
        )

    assert not any(
        "INSERT INTO wiki_page_links" in str(call.args[0])
        for call in connection.execute.call_args_list
    )


def test_restore_reactivates_deleted_page_without_linking_unrelated_deleted_page(
    monkeypatch,
) -> None:
    monkeypatch.setenv("S3_BUCKET", "wiki-bucket")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    def execute(query, params=None):
        cursor = Mock()
        normalized = " ".join(str(query).split())
        if normalized.startswith("SELECT id, page_type, slug, user_id"):
            cursor.fetchall.return_value = [{
                "id": "page-restored",
                "page_type": "concept",
                "slug": "restored",
                "user_id": "user-1",
            }]
        elif normalized.startswith("SELECT DISTINCT embedding_vector_id"):
            cursor.fetchall.return_value = []
        elif normalized.startswith("SELECT id FROM wiki_pages"):
            cursor.fetchone.return_value = None
        elif normalized.startswith("SELECT id, user_id FROM wiki_pages"):
            cursor.fetchall.return_value = [{
                "id": "page-deleted",
                "user_id": "user-1",
            }]
        return cursor

    connection.execute.side_effect = execute
    with (
        patch.object(database, "connect", return_value=connection),
        patch.object(database, "concept_write_lock", return_value=nullcontext()),
        patch.object(database, "read_text_object", return_value="# Restored\n"),
        patch.object(database, "invalidate_concept_index"),
    ):
        database.apply_restored_wiki_state_and_cleanup(
            "restore-1",
            "ws-1",
            [{
                "page_id": "page-restored",
                "markdown_key": "wiki/ws-1/pages/page-restored/ops/restore-1.md",
            }],
            {
                "removed_links": [],
                "restored_links": [{
                    "source": "concept:restored",
                    "target": "concept:deleted",
                    "relation": "related_to",
                }],
            },
            True,
            ["page-deleted"],
        )

    calls = connection.execute.call_args_list
    assert any(
        "status = 'active'" in str(call.args[0])
        and call.args[1][-2:] == ("page-restored", "ws-1")
        for call in calls
    )
    assert any("status = 'deleted'" in str(call.args[0]) for call in calls)
    assert not any("SELECT id FROM wiki_pages" in str(call.args[0]) for call in calls)
    assert not any("INSERT INTO wiki_page_links" in str(call.args[0]) for call in calls)


def test_restore_after_delete_and_rename_skips_stale_slug_link(monkeypatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "wiki-bucket")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    def execute(query, params=None):
        cursor = Mock()
        normalized = " ".join(str(query).split())
        if normalized.startswith("SELECT id, page_type, slug, user_id"):
            cursor.fetchall.return_value = [{
                "id": "page-renamed",
                "page_type": "concept",
                "slug": "renamed",
                "user_id": "user-1",
            }]
        elif normalized.startswith("SELECT DISTINCT embedding_vector_id"):
            cursor.fetchall.return_value = []
        elif normalized.startswith("SELECT id, user_id FROM wiki_pages"):
            cursor.fetchall.return_value = [{
                "id": "page-deleted",
                "user_id": "user-1",
            }]
        elif normalized.startswith("SELECT id FROM wiki_pages"):
            cursor.fetchone.return_value = {"id": "stale-page"}
        return cursor

    connection.execute.side_effect = execute
    with (
        patch.object(database, "connect", return_value=connection),
        patch.object(database, "concept_write_lock", return_value=nullcontext()),
        patch.object(database, "read_text_object", return_value="# Renamed\n"),
        patch.object(database, "invalidate_concept_index"),
    ):
        database.apply_restored_wiki_state_and_cleanup(
            "restore-1",
            "ws-1",
            [{
                "page_id": "page-renamed",
                "markdown_key": "wiki/ws-1/pages/page-renamed/ops/restore-1.md",
            }],
            {
                "removed_links": [],
                "restored_links": [{
                    "source": "concept:renamed",
                    "target": "concept:deleted-name",
                    "relation": "related_to",
                }],
            },
            True,
            ["page-deleted"],
        )

    calls = connection.execute.call_args_list
    assert not any("SELECT id FROM wiki_pages" in str(call.args[0]) for call in calls)
    assert not any("INSERT INTO wiki_page_links" in str(call.args[0]) for call in calls)


def test_restore_source_recreates_source_link_and_embedding_provenance_after_cleanup(
    monkeypatch,
) -> None:
    monkeypatch.setenv("S3_BUCKET", "wiki-bucket")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    def execute(query, params=None):
        cursor = Mock()
        normalized = " ".join(str(query).split())
        if normalized.startswith("SELECT id, page_type, slug, user_id"):
            cursor.fetchall.return_value = [{
                "id": "page-source",
                "page_type": "source",
                "slug": "renamed-source",
                "user_id": "user-1",
            }]
        elif normalized.startswith("SELECT DISTINCT embedding_vector_id"):
            cursor.fetchall.return_value = []
        return cursor

    connection.execute.side_effect = execute
    persist_units = Mock()
    with (
        patch.object(database, "connect", return_value=connection),
        patch.object(database, "read_text_object", return_value="# Source\n"),
        patch.object(database, "_persist_embedding_units", persist_units),
    ):
        database.apply_restored_wiki_state(
            "ws-1",
            [{
                "page_id": "page-source",
                "markdown_key": "wiki/ws-1/pages/page-source/ops/restore-1.md",
                "source_document_id": "doc-stable",
            }],
            {"removed_links": [], "restored_links": []},
            True,
        )

    persist_units.assert_called_once_with(
        connection,
        "page-source",
        "doc-stable",
        "# Source\n",
    )
    source_link_call = next(
        call
        for call in connection.execute.call_args_list
        if "relation_type, confidence, workspace_id" in str(call.args[0])
    )
    assert source_link_call.args[1] == (
        "doc-stable",
        "page-source",
        "source_of",
        1.0,
        "ws-1",
    )
    page_query = connection.execute.call_args_list[0].args[0]
    assert "relation_type = 'source_of'" in page_query


def test_restore_legacy_concept_skips_document_bound_embeddings(monkeypatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "wiki-bucket")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    def execute(query, params=None):
        cursor = Mock()
        normalized = " ".join(str(query).split())
        if normalized.startswith("SELECT id, page_type, slug, user_id"):
            cursor.fetchall.return_value = [{
                "id": "page-c3",
                "page_type": "concept",
                "slug": "c3",
                "user_id": "user-1",
            }]
        elif normalized.startswith("SELECT DISTINCT embedding_vector_id"):
            cursor.fetchall.return_value = []
        return cursor

    connection.execute.side_effect = execute
    persist_units = Mock()

    with (
        patch.object(database, "connect", return_value=connection),
        patch.object(database, "read_text_object", return_value="# Restored C3\n"),
        patch.object(database, "_persist_embedding_units", persist_units),
    ):
        database.apply_restored_wiki_state(
            "ws-1",
            [{
                "page_id": "page-c3",
                "markdown_key": "wiki/ws-1/pages/page-c3/ops/restore-1.md",
            }],
            {"removed_links": [], "restored_links": []},
            True,
        )

    persist_units.assert_not_called()


def test_restore_state_and_cleanup_share_lock_and_rollback(monkeypatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "wiki-bucket")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    def execute(query, params=None):
        cursor = Mock()
        normalized = " ".join(str(query).split())
        if normalized.startswith("SELECT id, page_type, slug, user_id"):
            cursor.fetchall.return_value = [{
                "id": "page-c3",
                "page_type": "source",
                "slug": "renamed-source",
                "user_id": "user-1",
            }]
        elif normalized.startswith("SELECT DISTINCT embedding_vector_id"):
            cursor.fetchall.return_value = []
        elif normalized.startswith("SELECT id, user_id FROM wiki_pages"):
            cursor.fetchall.return_value = [{"id": "page-deleted"}]
        elif normalized.startswith("UPDATE wiki_pages SET status = 'deleted'"):
            raise RuntimeError("cleanup failed")
        return cursor

    connection.execute.side_effect = execute
    with (
        patch.object(database, "connect", return_value=connection),
        patch.object(database, "concept_write_lock", return_value=nullcontext()) as lock,
        patch.object(database, "read_text_object", return_value="# Restored C3\n"),
        patch.object(database, "_persist_embedding_units"),
        patch.object(database, "invalidate_concept_index") as invalidate,
    ):
        with pytest.raises(RuntimeError, match="cleanup failed"):
            database.apply_restored_wiki_state_and_cleanup(
                "restore-1",
                "ws-1",
                [{
                    "page_id": "page-c3",
                    "markdown_key": "wiki/ws-1/pages/page-c3/ops/restore-1.md",
                    "source_document_id": "doc-A",
                }],
                {"removed_links": [], "restored_links": []},
                True,
                ["page-deleted"],
            )

    lock.assert_called_once_with("ws-1", "restore-1")
    assert connection.__enter__.call_count == 1
    assert connection.__exit__.call_args.args[0] is RuntimeError
    assert any(
        call.args[1] == (
            "doc-A",
            "page-c3",
            "source_of",
            1.0,
            "ws-1",
        )
        for call in connection.execute.call_args_list
        if "relation_type, confidence, workspace_id" in str(call.args[0])
    )
    invalidate.assert_not_called()


def test_delete_only_restore_invalidates_deleted_page_owner(monkeypatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "wiki-bucket")
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    def execute(query, params=None):
        cursor = Mock()
        normalized = " ".join(str(query).split())
        if normalized.startswith("SELECT id, user_id FROM wiki_pages"):
            cursor.fetchall.return_value = [
                {"id": "page-deleted", "user_id": "user-1"}
            ]
        elif normalized.startswith("SELECT DISTINCT embedding_vector_id"):
            cursor.fetchall.return_value = []
        return cursor

    connection.execute.side_effect = execute
    with (
        patch.object(database, "connect", return_value=connection),
        patch.object(database, "concept_write_lock", return_value=nullcontext()),
        patch.object(database, "invalidate_concept_index") as invalidate,
    ):
        database.apply_restored_wiki_state_and_cleanup(
            "restore-1",
            "ws-1",
            [],
            {"removed_links": [], "restored_links": []},
            False,
            ["page-deleted"],
        )

    invalidate.assert_called_once_with("user-1", "ws-1")


@pytest.mark.parametrize("status", ["cancel_requested", "rolling_back", "rollback_failed", "cancelled", "failed", "succeeded"])
def test_finish_rejects_inactive_run_before_persisting_outputs(status):
    from app.core.pipeline_control import PipelineRunCancelledError
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.execute.return_value.fetchone.return_value = {
        "document_id": "doc", "user_id": "user", "workspace_id": "ws", "status": status,
    }
    with (patch.object(database, "connect", return_value=connection),
          patch.object(database, "concept_write_lock", return_value=nullcontext()),
          patch.object(database, "_persist_wiki_outputs") as persist,
          patch.object(database, "_mark_derived_state_ingested") as derived):
        with pytest.raises(PipelineRunCancelledError):
            database.finish_pipeline_run("run", {})
    persist.assert_not_called()
    derived.assert_not_called()
