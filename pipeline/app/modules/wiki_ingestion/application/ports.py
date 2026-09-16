from collections.abc import Callable
from typing import Any, ContextManager, Protocol

from app.modules.wiki_ingestion.application.models import (
    PipelineRunCommand,
    RebuildPageCommand,
    SourceSnapshotRestoreCommand,
    WikiMaintenanceCommand,
)


class WikiMaintenancePort(Protocol):
    def lint(self, command: WikiMaintenanceCommand) -> dict[str, Any]: ...


class PipelineRunnerPort(Protocol):
    def run(
        self,
        command: PipelineRunCommand,
        progress_callback: Callable[[], bool | None] | None = None,
        finalization_callback: Callable[
            [Callable[[], dict[str, Any]]], dict[str, Any]
        ]
        | None = None,
    ) -> dict[str, Any]: ...


class PipelineRunRepositoryPort(Protocol):
    def create(
        self,
        run_id: str,
        document_id: str | None,
        user_id: str | None,
        workspace_id: str | None,
        input_source: str,
        output_dir: str,
        mode: str,
    ) -> None: ...

    def finish(
        self,
        run_id: str,
        manifest: dict[str, Any],
        expected_source_hash: str | None = None,
    ) -> list[str]: ...

    def concept_write_lock(
        self,
        workspace_id: str,
        run_id: str,
    ) -> ContextManager[None]: ...

    def fail(self, run_id: str, error: str) -> None: ...

    def touch(self, run_id: str) -> bool: ...

    def get_document(self, document_id: str) -> dict[str, Any] | None: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def list_active_concept_index(
        self,
        user_id: str,
        workspace_id: str,
    ) -> list[dict[str, Any]]: ...

    def latest_source_page_context(
        self,
        document_id: str,
        user_id: str,
        workspace_id: str,
    ) -> dict[str, Any] | None: ...

    def list_source_blocks(
        self,
        document_id: str,
    ) -> list[dict[str, Any]]: ...


class WikiEmbeddingJobPort(Protocol):
    def start(self, run_id: str, page_ids: list[str]) -> None: ...


class WikiPageRestorePort(Protocol):
    def apply_current_state_and_cleanup(
        self,
        operation_id: str,
        workspace_id: str,
        changed_pages: list[dict[str, Any]],
        link_changes: dict[str, list[dict[str, Any]]],
        replace_links: bool,
        deleted_page_ids: list[str],
    ) -> None: ...

    def rebuild_page(
        self,
        operation_id: str,
        workspace_id: str,
        page: RebuildPageCommand,
    ) -> dict[str, Any]: ...

    def restore_source_page(
        self,
        operation_id: str,
        restore_to_operation_id: str,
        workspace_id: str,
        source_page: SourceSnapshotRestoreCommand,
    ) -> dict[str, Any]: ...

    def calculate_lint_action_changes(
        self,
        target_operation_id: str,
        workspace_id: str,
        affected_page_ids: list[str],
        supported_links: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]: ...


class PipelineSourceReaderPort(Protocol):
    def read_text(self, object_uri: str) -> str: ...


class PipelineLogReaderPort(Protocol):
    def read_text(self, path: str) -> str: ...
