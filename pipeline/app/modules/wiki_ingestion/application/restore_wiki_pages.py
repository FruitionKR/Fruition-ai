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
            source_page = self._page_restore.restore_source_page(
                command.operation_id,
                command.restore_to_operation_id,
                command.workspace_id,
                command.source_page,
            )
            if source_page is not None:
                changed_pages.append(source_page)
        rebuilt_pages, replay_deleted = self._rebuild_pages(command)
        changed_pages.extend(rebuilt_pages)
        deleted_pages.extend(page_id for page_id in replay_deleted if page_id not in deleted_pages)
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
        changed_pages, replay_deleted = self._rebuild_pages(command)
        deleted_pages = list(dict.fromkeys([*command.deleted_pages, *replay_deleted]))
        affected_page_ids = [
            *[page.page_id for page in command.rebuild_pages],
            *command.deleted_pages,
        ]
        supported_links = [
            link
            for page in changed_pages
            for link in page.get("supported_links", [])
        ]
        link_changes = self._page_restore.calculate_lint_action_changes(
            command.target_operation_id,
            command.workspace_id,
            affected_page_ids,
            supported_links,
        )
        # 재조립한 페이지의 나가는 링크는 재생 결과(supported_links)로 바꾼다. lint가 원래 작업 id로 이은 기록이 낸
        # 링크(분리 때 옮긴 관계)는 lint 기록 차이에 없어, 차이만 반영하면 자식이 남을 때 그 링크가 남는다.
        # 사람이 낸 링크도 수동 기록 재생에 들어 있고, 소스 → 개념 링크는 동기화가 따로 맞춘다.
        skipped_page_ids = self._page_restore.apply_current_state_and_cleanup(
            command.operation_id,
            command.workspace_id,
            changed_pages,
            {
                "removed_links": link_changes["removed_links"],
                "restored_links": [*link_changes["restored_links"], *supported_links],
            },
            True,
            deleted_pages,
        )
        # lint가 승격·병합하며 archived.md로 옮긴 클러스터를 active.md로 되돌린다.
        # 되돌리지 않으면 지운 페이지의 클러스터가 보관된 채로 남아 다시 승격되지 않는다.
        cluster_changes = self._page_restore.restore_lint_clusters(
            command.operation_id,
            command.target_operation_id,
            command.workspace_id,
            affected_page_ids,
        )
        changed_pages = _without_pages(changed_pages, skipped_page_ids)
        result = self._result(
            operation_id=command.operation_id,
            operation_type="lint_restore",
            target_operation_id=command.target_operation_id,
            changed_pages=changed_pages,
            deleted_pages=deleted_pages,
            link_changes=link_changes,
            cluster_changes=cluster_changes,
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
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """재조립한 페이지와, 재생 결과 삭제로 끝난 페이지 id(ADR 0042)."""
        # 모든 페이지와 링크를 준비한 뒤 현재 상태를 한 번에 반영한다.
        # 준비 실패를 부분 성공으로 바꾸면 취소 대상의 일부만 남게 된다.
        pages = [
            self._page_restore.rebuild_page(
                command.operation_id,
                command.workspace_id,
                page,
            )
            for page in command.rebuild_pages
        ]
        return (
            [page for page in pages if not page.get("deleted")],
            [str(page["page_id"]) for page in pages if page.get("deleted")],
        )

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
