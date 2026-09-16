from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from app.modules.wiki_ingestion.application.models import (
    RebuildPageCommand,
    SourceSnapshotRestoreCommand,
)
from app.modules.wiki_ingestion.application.ports import WikiPageRestorePort
from app.modules.wiki_ingestion.domain.operation_recovery import PageRebuildError
from app.modules.wiki_ingestion.domain.lint_operation_recovery import (
    calculate_lint_link_changes,
)
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    load_concept_contributions,
    rebuild_concept_page,
)
from app.modules.wiki_ingestion.infrastructure.wiki_persistence_payload import (
    markdown_title,
)
from app.modules.wiki_ingestion.infrastructure.markdown_sections import markdown_section


class ObjectStorageWikiPageRestore(WikiPageRestorePort):
    def __init__(
        self,
        read_text: Callable[[str], str],
        write_text: Callable[[str, str, str], str],
        apply_current_state_and_cleanup: Callable[
            [
                str,
                str,
                list[dict[str, Any]],
                dict[str, list[dict[str, Any]]],
                bool,
                list[str],
            ],
            None,
        ],
    ) -> None:
        self._read_text = read_text
        self._write_text = write_text
        self._apply_current_state_and_cleanup = apply_current_state_and_cleanup

    def apply_current_state_and_cleanup(
        self,
        operation_id: str,
        workspace_id: str,
        changed_pages: list[dict[str, Any]],
        link_changes: dict[str, list[dict[str, Any]]],
        replace_links: bool,
        deleted_page_ids: list[str],
    ) -> None:
        self._apply_current_state_and_cleanup(
            operation_id,
            workspace_id,
            changed_pages,
            link_changes,
            replace_links,
            deleted_page_ids,
        )

    def rebuild_page(
        self,
        operation_id: str,
        workspace_id: str,
        page: RebuildPageCommand,
    ) -> dict[str, Any]:
        try:
            contributions = load_concept_contributions(
                workspace_id=workspace_id,
                page_id=page.page_id,
                keep_contributions=[
                    {
                        "operation_id": item.operation_id,
                        "sequence": sequence,
                    }
                    for sequence, item in enumerate(
                        page.keep_contributions,
                        start=1,
                    )
                ],
                read_text=self._read_text,
            )
            rebuilt = rebuild_concept_page(contributions)
            markdown_key = (
                f"wiki/{workspace_id}/pages/{page.page_id}/ops/"
                f"{operation_id}.md"
            )
            self._write_text(
                markdown_key,
                rebuilt.markdown,
                "text/markdown; charset=utf-8",
            )
        except PageRebuildError:
            raise
        except Exception as exc:
            raise PageRebuildError(
                f"failed to rebuild page:{page.page_id}"
            ) from exc
        digest = hashlib.sha256(rebuilt.markdown.encode("utf-8")).hexdigest()
        return {
            "page_id": page.page_id,
            "page_type": "concept",
            "markdown_key": markdown_key,
            "content_hash": f"sha256:{digest}",
            "supported_links": list(rebuilt.supported_links),
            "title": rebuilt.title,
            "summary": rebuilt.summary,
            "source_document_ids": list(rebuilt.source_document_ids),
        }

    def restore_source_page(
        self,
        operation_id: str,
        restore_to_operation_id: str,
        workspace_id: str,
        source_page: SourceSnapshotRestoreCommand,
    ) -> dict[str, Any]:
        source_key = (
            f"wiki/{workspace_id}/pages/{source_page.page_id}/ops/"
            f"{restore_to_operation_id}.md"
        )
        target_key = (
            f"wiki/{workspace_id}/pages/{source_page.page_id}/ops/"
            f"{operation_id}.md"
        )
        try:
            markdown = self._read_text(source_key)
            self._write_text(
                target_key,
                markdown,
                "text/markdown; charset=utf-8",
            )
        except Exception as exc:
            raise PageRebuildError(
                f"failed to restore source snapshot:{source_page.page_id}"
            ) from exc
        digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        return {
            "page_id": source_page.page_id,
            "page_type": "source",
            "markdown_key": target_key,
            "content_hash": f"sha256:{digest}",
            "title": markdown_title(markdown),
            "summary": markdown_section(markdown, "Summary"),
            "source_document_id": source_page.document_id,
        }

    def calculate_lint_action_changes(
        self,
        target_operation_id: str,
        workspace_id: str,
        affected_page_ids: list[str],
        supported_links: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        contributions = []
        try:
            for page_id in dict.fromkeys(affected_page_ids):
                key = (
                    f"wiki/{workspace_id}/pages/{page_id}/ops/"
                    f"{target_operation_id}.json"
                )
                contribution = json.loads(self._read_text(key))
                if (
                    contribution.get("artifact_type") != "lint"
                    or contribution.get("operation_id") != target_operation_id
                    or contribution.get("page_id") != page_id
                ):
                    raise ValueError("lint contribution identity does not match")
                contributions.append(contribution)
        except Exception as exc:
            raise PageRebuildError(
                f"failed to restore lint actions:{target_operation_id}"
            ) from exc
        return calculate_lint_link_changes(contributions, supported_links)
