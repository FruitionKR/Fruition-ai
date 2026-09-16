import json

import pytest

from app.modules.wiki_ingestion.domain.operation_recovery import PageRebuildError

from app.modules.wiki_ingestion.application.models import (
    IngestOperationRestoreCommand,
    LintOperationRestoreCommand,
    RebuildPageCommand,
    RestoreContributionCommand,
    SourceSnapshotRestoreCommand,
)
from app.modules.wiki_ingestion.application.restore_wiki_pages import (
    RestoreWikiPagesUseCase,
)
from app.modules.wiki_ingestion.infrastructure.wiki_page_restore import (
    ObjectStorageWikiPageRestore,
)


def _payload(operation_id: str, page_id: str, evidence: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "operation_id": operation_id,
            "document_id": f"doc-{operation_id}",
            "page_id": page_id,
            "concept": {
                "slug": page_id.lower(),
                "title": page_id,
                "definition": evidence,
                "source_document_ids": [f"doc-{operation_id}"],
                "evidence_claim_ids": [f"ev-{operation_id}"],
            },
            "evidence_units": [
                {
                    "evidence_id": f"ev-{operation_id}",
                    "claim": evidence,
                    "anchor_reference_ids": [],
                    "related_concept_slugs": [page_id.lower()],
                    "source_document_id": f"doc-{operation_id}",
                }
            ],
            "source_blocks": [],
            "links": [],
        },
        ensure_ascii=False,
    )


def _lint_payload(
    operation_id: str,
    page_id: str,
    evidence: str,
    *,
    added_links: list[dict] | None = None,
    removed_links: list[dict] | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "artifact_type": "lint",
            "operation_id": operation_id,
            "document_id": f"lint:{operation_id}",
            "page_id": page_id,
            "content_action": "append_evidence",
            "concept": {
                "slug": page_id.lower(),
                "title": page_id,
                "definition": "",
                "source_document_ids": [],
                "evidence_claim_ids": [f"ev-{operation_id}"],
            },
            "evidence_units": [
                {
                    "evidence_id": f"ev-{operation_id}",
                    "claim": evidence,
                    "anchor_reference_ids": [],
                    "related_concept_slugs": [page_id.lower()],
                    "source_document_id": "",
                }
            ],
            "source_blocks": [],
            "source_key_points": [],
            "added_links": added_links or [],
            "removed_links": removed_links or [],
        },
        ensure_ascii=False,
    )


class ArtifactStore:
    def __init__(self, objects: dict[str, str]) -> None:
        self.objects = objects
        self.writes: list[tuple[str, str, str]] = []
        self.cleaned_pages: list[tuple[str, list[str]]] = []
        self.current_states: list[tuple] = []

    def read_text(self, key: str) -> str:
        return self.objects[key]

    def write_text(self, key: str, text: str, content_type: str) -> str:
        self.writes.append((key, text, content_type))
        return f"s3://bucket/{key}"

    def apply_current_state_and_cleanup(
        self,
        operation_id: str,
        workspace_id: str,
        changed_pages: list[dict],
        link_changes: dict[str, list[dict]],
        replace_links: bool,
        deleted_page_ids: list[str],
    ) -> None:
        self.current_states.append(
            (workspace_id, changed_pages, link_changes, replace_links)
        )
        if deleted_page_ids:
            self.cleaned_pages.append((workspace_id, deleted_page_ids))


class EmbeddingJob:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def start(self, run_id: str, page_ids: list[str]) -> None:
        self.calls.append((run_id, page_ids))


class StorageReadError(Exception):
    pass


def _restore(store: ArtifactStore) -> ObjectStorageWikiPageRestore:
    return ObjectStorageWikiPageRestore(
        store.read_text,
        store.write_text,
        store.apply_current_state_and_cleanup,
    )


def test_restore_rebuilds_page_from_selected_contribution_json() -> None:
    store = ArtifactStore(
        {
            "wiki/ws-1/pages/C3/ops/A.json": _payload("A", "C3", "A 근거"),
            "wiki/ws-1/pages/C3/ops/B.json": _payload("B", "C3", "B 근거"),
        }
    )
    embedding_job = EmbeddingJob()
    use_case = RestoreWikiPagesUseCase(_restore(store), embedding_job)

    result = use_case.execute_ingest(
        IngestOperationRestoreCommand(
            operation_id="restore-1",
            restore_to_operation_id=None,
            cancel_operation_ids=("target-ingest",),
            workspace_id="ws-1",
            source_page=SourceSnapshotRestoreCommand("S1", "doc-source"),
            rebuild_pages=(
                RebuildPageCommand(
                    page_id="C3",
                    keep_contributions=(
                        RestoreContributionCommand("A", "doc-A"),
                        RestoreContributionCommand("B", "doc-B"),
                    ),
                ),
            ),
        )
    )

    assert result["status"] == "succeeded"
    assert result["failed_pages"] == []
    assert result["changed_pages"][0]["page_id"] == "C3"
    assert store.writes[0][0] == (
        "wiki/ws-1/pages/C3/ops/restore-1.md"
    )
    assert "A 근거" in store.writes[0][1]
    assert "B 근거" in store.writes[0][1]
    assert store.current_states[0][0] == "ws-1"
    assert store.current_states[0][1] == result["changed_pages"]
    assert store.current_states[0][3] is True
    assert embedding_job.calls == [("restore-1", ["C3"])]


def test_restore_changed_page_carries_restored_metadata_and_contributions() -> None:
    store = ArtifactStore(
        {
            "wiki/ws-1/pages/C3/ops/A.json": _payload("A", "C3", "A 근거"),
        }
    )
    result = RestoreWikiPagesUseCase(_restore(store)).execute_ingest(
        IngestOperationRestoreCommand(
            operation_id="restore-1",
            restore_to_operation_id=None,
            cancel_operation_ids=("target-ingest",),
            workspace_id="ws-1",
            source_page=SourceSnapshotRestoreCommand("S1", "doc-source"),
            rebuild_pages=(
                RebuildPageCommand(
                    page_id="C3",
                    keep_contributions=(
                        RestoreContributionCommand("A", "doc-A"),
                    ),
                ),
            ),
        )
    )

    page = result["changed_pages"][0]
    assert page["title"] == "C3"
    assert page["summary"] == "A 근거"
    assert page["source_document_ids"] == ["doc-A"]


def test_restore_legacy_contribution_without_document_id_keeps_embedding_unbound() -> None:
    legacy = json.loads(_payload("A", "C3", "A 근거"))
    legacy.pop("document_id")
    legacy["concept"].pop("source_document_ids")
    legacy["evidence_units"][0].pop("source_document_id")
    store = ArtifactStore(
        {"wiki/ws-1/pages/C3/ops/A.json": json.dumps(legacy)}
    )

    result = RestoreWikiPagesUseCase(_restore(store)).execute_ingest(
        IngestOperationRestoreCommand(
            operation_id="restore-1",
            restore_to_operation_id=None,
            cancel_operation_ids=("target-ingest",),
            workspace_id="ws-1",
            source_page=SourceSnapshotRestoreCommand("S1", "doc-source"),
            rebuild_pages=(
                RebuildPageCommand(
                    page_id="C3",
                    keep_contributions=(
                        RestoreContributionCommand("A", "doc-A"),
                    ),
                ),
            ),
        )
    )

    assert result["changed_pages"][0]["source_document_ids"] == []


def test_restore_lint_only_contribution_keeps_document_provenance_unbound() -> None:
    lint = json.loads(_lint_payload("L", "C3", "lint 근거"))
    lint["concept"]["source_document_ids"] = ["doc-A"]
    lint["evidence_units"][0]["source_document_id"] = "doc-A"
    store = ArtifactStore(
        {"wiki/ws-1/pages/C3/ops/L.json": json.dumps(lint)}
    )

    result = RestoreWikiPagesUseCase(_restore(store)).execute_lint(
        LintOperationRestoreCommand(
            operation_id="restore-1",
            target_operation_id="L",
            workspace_id="ws-1",
            rebuild_pages=(
                RebuildPageCommand(
                    page_id="C3",
                    keep_contributions=(
                        RestoreContributionCommand("L", "lint:L"),
                    ),
                ),
            ),
        )
    )

    assert result["changed_pages"][0]["source_document_ids"] == []


def test_restore_ingest_falls_back_to_document_id() -> None:
    legacy = json.loads(_payload("A", "C3", "A 근거"))
    legacy["concept"].pop("source_document_ids")
    legacy["evidence_units"][0].pop("source_document_id")
    store = ArtifactStore(
        {"wiki/ws-1/pages/C3/ops/A.json": json.dumps(legacy)}
    )

    result = RestoreWikiPagesUseCase(_restore(store)).execute_ingest(
        IngestOperationRestoreCommand(
            operation_id="restore-1",
            restore_to_operation_id=None,
            cancel_operation_ids=("target-ingest",),
            workspace_id="ws-1",
            source_page=SourceSnapshotRestoreCommand("S1", "doc-source"),
            rebuild_pages=(
                RebuildPageCommand(
                    page_id="C3",
                    keep_contributions=(
                        RestoreContributionCommand("A", "doc-A"),
                    ),
                ),
            ),
        )
    )

    assert result["changed_pages"][0]["source_document_ids"] == ["doc-A"]


def test_restore_replays_ingest_and_lint_artifacts_in_operation_order() -> None:
    first_link = {
        "source": "concept:c3",
        "target": "concept:first",
        "relation": "related_to",
    }
    lint_link = {
        "source": "concept:c3",
        "target": "concept:lint",
        "relation": "related_to",
    }
    ingest_a = json.loads(_payload("A", "C3", "A 근거"))
    ingest_a["links"] = [first_link]
    store = ArtifactStore(
        {
            "wiki/ws-1/pages/C3/ops/A.json": json.dumps(ingest_a),
            "wiki/ws-1/pages/C3/ops/L.json": _lint_payload(
                "L",
                "C3",
                "lint 근거",
                added_links=[lint_link],
                removed_links=[first_link],
            ),
            "wiki/ws-1/pages/C3/ops/B.json": _payload("B", "C3", "B 근거"),
        }
    )
    use_case = RestoreWikiPagesUseCase(
        _restore(store)
    )

    result = use_case.execute_ingest(
        IngestOperationRestoreCommand(
            operation_id="restore-1",
            restore_to_operation_id=None,
            cancel_operation_ids=("target-ingest",),
            workspace_id="ws-1",
            source_page=SourceSnapshotRestoreCommand("S1", "doc-source"),
            rebuild_pages=(
                RebuildPageCommand(
                    page_id="C3",
                    keep_contributions=(
                        RestoreContributionCommand("A", "doc-A"),
                        RestoreContributionCommand("L", "lint:L"),
                        RestoreContributionCommand("B", "doc-B"),
                    ),
                ),
            ),
        )
    )

    assert result["status"] == "succeeded"
    assert "A 근거" in store.writes[0][1]
    assert "lint 근거" in store.writes[0][1]
    assert "B 근거" in store.writes[0][1]
    assert result["changed_pages"][0]["supported_links"] == [lint_link]


def test_restore_page_failure_prevents_all_current_state_changes() -> None:
    store = ArtifactStore(
        {
            "wiki/ws-1/pages/C3/ops/A.json": _payload("A", "C3", "A 근거"),
        }
    )
    embedding_job = EmbeddingJob()
    use_case = RestoreWikiPagesUseCase(_restore(store), embedding_job)

    with pytest.raises(PageRebuildError):
        use_case.execute_ingest(
            IngestOperationRestoreCommand(
                operation_id="restore-1",
                restore_to_operation_id=None,
                cancel_operation_ids=("target-ingest",),
                workspace_id="ws-1",
                source_page=SourceSnapshotRestoreCommand("S1", "doc-source"),
                rebuild_pages=(
                    RebuildPageCommand(
                        page_id="C3",
                        keep_contributions=(
                            RestoreContributionCommand("A", "doc-A"),
                        ),
                    ),
                    RebuildPageCommand(
                        page_id="C6",
                        keep_contributions=(
                            RestoreContributionCommand("D", "doc-D"),
                        ),
                    ),
                ),
            )
        )

    assert store.current_states == []
    assert store.cleaned_pages == []
    assert embedding_job.calls == []


def test_restore_treats_object_storage_error_as_page_failure() -> None:
    class FailingStore(ArtifactStore):
        def read_text(self, key: str) -> str:
            raise StorageReadError(key)

    store = FailingStore({})
    use_case = RestoreWikiPagesUseCase(
        _restore(store)
    )

    with pytest.raises(PageRebuildError):
        use_case.execute_ingest(
            IngestOperationRestoreCommand(
                operation_id="restore-1",
                restore_to_operation_id=None,
                cancel_operation_ids=("target-ingest",),
                workspace_id="ws-1",
                source_page=SourceSnapshotRestoreCommand("S1", "doc-source"),
                rebuild_pages=(
                    RebuildPageCommand(
                        page_id="C3",
                        keep_contributions=(
                            RestoreContributionCommand("A", "doc-A"),
                        ),
                    ),
                ),
            )
        )

    assert store.current_states == []
    assert store.cleaned_pages == []


def test_restore_treats_malformed_contribution_as_page_failure() -> None:
    store = ArtifactStore(
        {
            "wiki/ws-1/pages/C3/ops/A.json": json.dumps(
                {"operation_id": "A", "page_id": "C3"}
            ),
        }
    )
    use_case = RestoreWikiPagesUseCase(
        _restore(store)
    )

    with pytest.raises(PageRebuildError):
        use_case.execute_ingest(
            IngestOperationRestoreCommand(
                operation_id="restore-1",
                restore_to_operation_id=None,
                cancel_operation_ids=("target-ingest",),
                workspace_id="ws-1",
                source_page=SourceSnapshotRestoreCommand("S1", "doc-source"),
                rebuild_pages=(
                    RebuildPageCommand(
                        page_id="C3",
                        keep_contributions=(
                            RestoreContributionCommand("A", "doc-A"),
                        ),
                    ),
                ),
            )
        )

    assert store.current_states == []
    assert store.cleaned_pages == []


def test_ingest_restore_without_previous_source_marks_source_deleted() -> None:
    store = ArtifactStore({})
    use_case = RestoreWikiPagesUseCase(_restore(store))

    result = use_case.execute_ingest(
        IngestOperationRestoreCommand(
            operation_id="restore-1",
            restore_to_operation_id=None,
            cancel_operation_ids=("target-ingest",),
            workspace_id="ws-1",
            source_page=SourceSnapshotRestoreCommand("S_A", "doc-source"),
            rebuild_pages=(),
            deleted_pages=("C1", "C5"),
        )
    )

    assert result["changed_pages"] == []
    assert result["deleted_pages"] == ["C1", "C5", "S_A"]
    assert store.cleaned_pages == [("ws-1", ["C1", "C5", "S_A"])]


def test_ingest_operation_restore_returns_from_a5_to_a2() -> None:
    store = ArtifactStore(
        {
            "wiki/ws-1/pages/S1/ops/A2.md": "# A2 Source\n",
            "wiki/ws-1/pages/X/ops/A2.json": _payload("A2", "X", "A2의 X"),
            "wiki/ws-1/pages/Y/ops/B1.json": _payload("B1", "Y", "다른 문서의 Y"),
        }
    )
    use_case = RestoreWikiPagesUseCase(_restore(store))

    result = use_case.execute_ingest(
        IngestOperationRestoreCommand(
            operation_id="restore-1",
            restore_to_operation_id="A2",
            cancel_operation_ids=("A3", "A4", "A5"),
            workspace_id="ws-1",
            source_page=SourceSnapshotRestoreCommand("S1", "doc-source"),
            rebuild_pages=(
                RebuildPageCommand(
                    page_id="X",
                    keep_contributions=(
                        RestoreContributionCommand("A2", "doc-A"),
                    ),
                ),
                RebuildPageCommand(
                    page_id="Y",
                    keep_contributions=(
                        RestoreContributionCommand("B1", "doc-B"),
                    ),
                ),
            ),
            deleted_pages=("Z",),
        )
    )

    assert result["operation_type"] == "ingest_restore"
    assert result["restore_to_operation_id"] == "A2"
    assert result["cancel_operation_ids"] == ["A3", "A4", "A5"]
    assert [item["page_type"] for item in result["changed_pages"]] == [
        "source",
        "concept",
        "concept",
    ]
    assert result["deleted_pages"] == ["Z"]
    assert store.writes[0][0] == "wiki/ws-1/pages/S1/ops/restore-1.md"
    assert store.writes[0][1] == "# A2 Source\n"


def test_source_restore_carries_title_and_summary_metadata() -> None:
    store = ArtifactStore(
        {"wiki/ws-1/pages/S1/ops/A.md": "# A 제목\n\n## Summary\nA 요약\n"}
    )

    result = RestoreWikiPagesUseCase(_restore(store)).execute_ingest(
        IngestOperationRestoreCommand(
            operation_id="restore-1",
            restore_to_operation_id="A",
            cancel_operation_ids=("B",),
            workspace_id="ws-1",
            source_page=SourceSnapshotRestoreCommand("S1", "doc-source"),
            rebuild_pages=(),
        )
    )

    assert result["changed_pages"][0]["title"] == "A 제목"
    assert result["changed_pages"][0]["summary"] == "A 요약"


def test_lint_operation_restore_replays_remaining_link_support() -> None:
    restored_link = {
        "source": "concept:c3",
        "target": "concept:restored",
        "relation": "related_to",
    }
    still_supported_link = {
        "source": "concept:c3",
        "target": "concept:shared",
        "relation": "related_to",
    }
    ingest = json.loads(_payload("A", "C3", "남은 근거"))
    ingest["links"] = [restored_link, still_supported_link]
    store = ArtifactStore(
        {
            "wiki/ws-1/pages/C3/ops/A.json": json.dumps(ingest),
            "wiki/ws-1/pages/C3/ops/lint-B.json": _lint_payload(
                "lint-B",
                "C3",
                "취소할 lint 근거",
                added_links=[still_supported_link],
                removed_links=[restored_link],
            ),
        }
    )
    use_case = RestoreWikiPagesUseCase(
        _restore(store)
    )

    result = use_case.execute_lint(
        LintOperationRestoreCommand(
            operation_id="restore-2",
            target_operation_id="lint-B",
            workspace_id="ws-1",
            rebuild_pages=(
                RebuildPageCommand(
                    page_id="C3",
                    keep_contributions=(
                        RestoreContributionCommand("A", "doc-A"),
                    ),
                ),
            ),
        )
    )

    assert result["operation_type"] == "lint_restore"
    assert result["link_changes"] == {
        "removed_links": [],
        "restored_links": [restored_link],
    }
    assert "취소할 lint 근거" not in store.writes[0][1]


def test_lint_operation_restore_removes_unsupported_added_link() -> None:
    lint_link = {
        "source": "concept:c3",
        "target": "concept:lint-only",
        "relation": "related_to",
    }
    store = ArtifactStore(
        {
            "wiki/ws-1/pages/C3/ops/A.json": _payload("A", "C3", "남은 근거"),
            "wiki/ws-1/pages/C3/ops/lint-B.json": _lint_payload(
                "lint-B",
                "C3",
                "취소할 lint 근거",
                added_links=[lint_link],
            ),
        }
    )
    use_case = RestoreWikiPagesUseCase(
        _restore(store)
    )

    result = use_case.execute_lint(
        LintOperationRestoreCommand(
            operation_id="restore-2",
            target_operation_id="lint-B",
            workspace_id="ws-1",
            rebuild_pages=(
                RebuildPageCommand(
                    page_id="C3",
                    keep_contributions=(
                        RestoreContributionCommand("A", "doc-A"),
                    ),
                ),
            ),
        )
    )

    assert result["link_changes"] == {
        "removed_links": [lint_link],
        "restored_links": [],
    }


def test_lint_rebuild_failure_prevents_deletion_and_link_changes() -> None:
    store = ArtifactStore(
        {
            "wiki/ws-1/pages/C3/ops/lint-B.json": _lint_payload(
                "lint-B",
                "C3",
                "취소할 lint 근거",
                added_links=[
                    {
                        "source": "concept:c3",
                        "target": "concept:lint-only",
                        "relation": "related_to",
                    }
                ],
            ),
        }
    )
    use_case = RestoreWikiPagesUseCase(
        _restore(store)
    )

    with pytest.raises(PageRebuildError):
        use_case.execute_lint(
            LintOperationRestoreCommand(
                operation_id="restore-2",
                target_operation_id="lint-B",
                workspace_id="ws-1",
                deleted_pages=("Z",),
                rebuild_pages=(
                    RebuildPageCommand(
                        page_id="C3",
                        keep_contributions=(
                            RestoreContributionCommand("missing", "doc-A"),
                        ),
                    ),
                ),
            )
        )

    assert store.current_states == []
    assert store.cleaned_pages == []


def test_lint_operation_restore_reports_missing_target_log() -> None:
    store = ArtifactStore(
        {
            "wiki/ws-1/pages/C3/ops/A.json": _payload("A", "C3", "남은 근거"),
        }
    )
    use_case = RestoreWikiPagesUseCase(
        _restore(store)
    )

    with pytest.raises(PageRebuildError):
        use_case.execute_lint(
            LintOperationRestoreCommand(
                operation_id="restore-2",
                target_operation_id="lint-missing",
                workspace_id="ws-1",
                rebuild_pages=(
                    RebuildPageCommand(
                        page_id="C3",
                        keep_contributions=(
                            RestoreContributionCommand("A", "doc-A"),
                        ),
                    ),
                ),
            )
        )

    assert store.current_states == []
    assert store.cleaned_pages == []



def test_missing_source_snapshot_prevents_concept_changes_and_deletion() -> None:
    store = ArtifactStore({
        "wiki/ws-1/pages/C3/ops/A.json": _payload("A", "C3", "남은 근거"),
    })
    embedding_job = EmbeddingJob()
    use_case = RestoreWikiPagesUseCase(_restore(store), embedding_job)
    command = IngestOperationRestoreCommand(
        operation_id="restore-1",
        restore_to_operation_id="missing",
        cancel_operation_ids=("B",),
        workspace_id="ws-1",
        source_page=SourceSnapshotRestoreCommand("S1", "doc-source"),
        rebuild_pages=(
            RebuildPageCommand("C3", (RestoreContributionCommand("A", "doc-A"),)),
        ),
        deleted_pages=("Z",),
    )

    with pytest.raises(PageRebuildError, match="source snapshot"):
        use_case.execute_ingest(command)

    assert store.writes == []
    assert store.current_states == []
    assert store.cleaned_pages == []
    assert embedding_job.calls == []
