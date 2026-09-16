from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any


ArtifactWriter = Callable[[str, str, str], str]


def persist_operation_artifacts(
    *,
    operation_id: str,
    workspace_id: str,
    source_page_id: str,
    source_markdown: str,
    concept_pages: list[dict[str, Any]],
    concept_contributions: dict[str, dict[str, Any]],
    write_text: ArtifactWriter,
) -> list[dict[str, Any]]:
    source_key = _object_key(
        workspace_id,
        source_page_id,
        operation_id,
        "md",
    )
    write_text(source_key, source_markdown, "text/markdown; charset=utf-8")
    artifacts = [
        {
            "page_id": source_page_id,
            "page_type": "source",
            "markdown_key": source_key,
            "content_hash": _content_hash(source_markdown),
        }
    ]
    for page in concept_pages:
        page_id = str(page["page_id"])
        slug = str(page["slug"])
        markdown = str(page["markdown"])
        contribution = concept_contributions.get(slug)
        if contribution is None:
            raise ValueError(
                f"missing concept contribution JSON for concept:{slug}"
            )
        markdown_key = _object_key(
            workspace_id,
            page_id,
            operation_id,
            "md",
        )
        contribution_key = _object_key(
            workspace_id,
            page_id,
            operation_id,
            "json",
        )
        contribution_json = json.dumps(
            {
                **contribution,
                "page_id": page_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        write_text(
            markdown_key,
            markdown,
            "text/markdown; charset=utf-8",
        )
        write_text(
            contribution_key,
            contribution_json,
            "application/json; charset=utf-8",
        )
        artifacts.append(
            {
                "page_id": page_id,
                "page_type": "concept",
                "markdown_key": markdown_key,
                "contribution_key": contribution_key,
                "content_hash": _content_hash(markdown),
            }
        )
    return artifacts


def _object_key(
    workspace_id: str,
    page_id: str,
    operation_id: str,
    extension: str,
) -> str:
    return (
        f"wiki/{workspace_id}/pages/{page_id}/ops/"
        f"{operation_id}.{extension}"
    )


def _content_hash(markdown: str) -> str:
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
