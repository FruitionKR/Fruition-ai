from unittest.mock import Mock, patch

import pytest

from app.modules.wiki_ingestion.application import restore_wiki_pages as restore
from app.modules.wiki_ingestion.application.models import (
    IngestOperationRestoreCommand,
    LintOperationRestoreCommand,
    RebuildPageCommand,
    SourceSnapshotRestoreCommand,
)


@pytest.mark.parametrize("kind", ["ingest", "lint"])
def test_publication_keeps_existing_restore_response_contract(monkeypatch, kind):
    monkeypatch.setattr(restore, "prepare_publication", Mock())
    page = {"page_id": "page", "page_type": "concept", "source_document_ids": ["doc"],
            "supported_links": [], "title": "Concept", "summary": "Definition"}
    port = Mock()
    port.rebuild_page.return_value = page
    port.calculate_lint_action_changes.return_value = {"removed_links": [], "restored_links": []}
    port.restore_lint_clusters.return_value = {"restored": ["cluster"]}
    events = []

    def publish(_operation, _workspace, _pages, _links, _replace, _deleted, publication, pending):
        publication["event"] = {"result": dict(publication["result"])}
        events.append(publication["event"])
        return []

    port.apply_current_state_and_cleanup.side_effect = publish
    use_case = restore.RestoreWikiPagesUseCase(port)
    common = dict(operation_id="restore", workspace_id="workspace",
                  rebuild_pages=(RebuildPageCommand("page", ()),))
    if kind == "ingest":
        result = use_case.execute_ingest(IngestOperationRestoreCommand(
            **common, restore_to_operation_id=None, cancel_operation_ids=("old",),
            source_page=SourceSnapshotRestoreCommand("source", "doc"),
        ), user_id="user")
        assert result["restore_to_operation_id"] is None
        assert result["cancel_operation_ids"] == ["old"]
    else:
        result = use_case.execute_lint(LintOperationRestoreCommand(
            **common, target_operation_id="old",
        ), user_id="user")
        assert result["target_operation_id"] == "old"
        assert result["cluster_changes"] == {"restored": ["cluster"]}
    assert result["operation_type"] == f"{kind}_restore"
    assert result["changed_pages"] == [page]
    restore.prepare_publication.assert_called_once_with("restore", "workspace", "user", "RESTORE")
    assert events[0]["result"]["user_id"] == "user"
    assert events[0]["result"]["operation_type"] == "restore"
    assert "source_document_ids" not in events[0]["result"]["changed_pages"][0]
    # The HTTP envelope remains enriched, but both Kafka delivery paths must carry
    # the exact committed result, including when the journal replays this response.
    from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
    from app.workers import task_worker
    with patch.object(journal, "execute", return_value=result):
        task_result = task_worker._execute_controlled({"kind": f"restore_{kind}", "run_id": "run"})
    assert task_result == events[0]["result"]
