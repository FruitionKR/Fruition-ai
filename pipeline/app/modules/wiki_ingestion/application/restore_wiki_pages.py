from __future__ import annotations

from typing import Any

from app.modules.wiki_ingestion.application.models import (
    IngestOperationRestoreCommand,
    LintOperationRestoreCommand,
)
from app.modules.wiki_ingestion.application.ports import (
    WikiEmbeddingJobPort,
    WikiPageRestorePort,
)


class RestoreWikiPagesUseCase:
    def __init__(
        self,
        page_restore: WikiPageRestorePort,
        embedding_job: WikiEmbeddingJobPort | None = None,
    ) -> None:
        self._page_restore = page_restore
        self._embedding_job = embedding_job

    def execute_ingest(
        self,
        command: IngestOperationRestoreCommand,
    ) -> dict[str, Any]:
        changed_pages: list[dict[str, Any]] = []
        deleted_pages = list(command.deleted_pages)
        if command.restore_to_operation_id is None:
            if command.source_page.page_id not in deleted_pages:
                deleted_pages.append(command.source_page.page_id)
        else:
            changed_pages.append(
                self._page_restore.restore_source_page(
                    command.operation_id,
                    command.restore_to_operation_id,
                    command.workspace_id,
                    command.source_page,
                )
            )
        changed_pages.extend(self._rebuild_pages(command))
        skipped_page_ids = self._page_restore.apply_current_state_and_cleanup(
            command.operation_id,
            command.workspace_id,
            changed_pages,
            {
                "removed_links": [],
                "restored_links": [
                    link
                    for page in changed_pages
                    for link in page.get("supported_links", [])
                ],
            },
            True,
            deleted_pages,
        )
        # 삭제한 원본이라 되살리지 않은 페이지는 복구 결과로 보고하지 않는다.
        # 보고하면 document-svc가 그 페이지의 복구 버전을 기록해 AI 상태와 어긋난다.
        changed_pages = _without_pages(changed_pages, skipped_page_ids)
        result = self._result(
            operation_id=command.operation_id,
            operation_type="ingest_restore",
            restore_to_operation_id=command.restore_to_operation_id,
            cancel_operation_ids=list(command.cancel_operation_ids),
            changed_pages=changed_pages,
            deleted_pages=deleted_pages,
        )
        self._start_embeddings(command.operation_id, changed_pages)
        return result

    def execute_lint(
        self,
        command: LintOperationRestoreCommand,
    ) -> dict[str, Any]:
        changed_pages = self._rebuild_pages(command)
        supported_links = [
            link
            for page in changed_pages
            for link in page.get("supported_links", [])
        ]
        link_changes = self._page_restore.calculate_lint_action_changes(
            command.target_operation_id,
            command.workspace_id,
            [
                *[page.page_id for page in command.rebuild_pages],
                *command.deleted_pages,
            ],
            supported_links,
        )
        skipped_page_ids = self._page_restore.apply_current_state_and_cleanup(
            command.operation_id,
            command.workspace_id,
            changed_pages,
            link_changes,
            False,
            list(command.deleted_pages),
        )
        changed_pages = _without_pages(changed_pages, skipped_page_ids)
        result = self._result(
            operation_id=command.operation_id,
            operation_type="lint_restore",
            target_operation_id=command.target_operation_id,
            changed_pages=changed_pages,
            deleted_pages=list(command.deleted_pages),
            link_changes=link_changes,
            failed_actions=[],
        )
        self._start_embeddings(command.operation_id, changed_pages)
        return result

    def _start_embeddings(
        self,
        run_id: str,
        changed_pages: list[dict[str, Any]],
    ) -> None:
        if self._embedding_job is not None:
            self._embedding_job.start(
                run_id,
                [str(page["page_id"]) for page in changed_pages],
            )

    def _rebuild_pages(
        self,
        command: IngestOperationRestoreCommand | LintOperationRestoreCommand,
    ) -> list[dict[str, Any]]:
        # 모든 페이지와 링크를 준비한 뒤 현재 상태를 한 번에 반영한다.
        # 준비 실패를 부분 성공으로 바꾸면 취소 대상의 일부만 남게 된다.
        return [
            self._page_restore.rebuild_page(
                command.operation_id,
                command.workspace_id,
                page,
            )
            for page in command.rebuild_pages
        ]

    @staticmethod
    def _result(
        *,
        operation_id: str,
        operation_type: str,
        changed_pages: list[dict[str, Any]],
        **values: Any,
    ) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "operation_type": operation_type,
            "status": "succeeded",
            "changed_pages": changed_pages,
            "failed_pages": [],
            **values,
        }


def _without_pages(
    pages: list[dict[str, Any]],
    page_ids: list[str],
) -> list[dict[str, Any]]:
    excluded = set(page_ids)
    return [page for page in pages if str(page["page_id"]) not in excluded]
