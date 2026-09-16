from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any


ArtifactWriter = Callable[[str, str, str], str]


def persist_lint_operation_artifacts(
    *,
    operation_id: str,
    workspace_id: str,
    page_changes: list[dict[str, Any]],
    write_text: ArtifactWriter,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for change in page_changes:
        action = str(change.get("content_action") or "none")
        claims = [
            claim
            for claim in change.get("claims", [])
            if isinstance(claim, dict)
        ]
        added_links = _dict_items(change.get("added_links", []))
        removed_links = _dict_items(change.get("removed_links", []))
        if action not in {"create", "append_evidence"} and not (
            added_links or removed_links
        ):
            raise ValueError("lint artifact must contain a replayable action")

        page_id = str(change["page_id"])
        markdown = str(change["markdown"])
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
        payload = _lint_payload(
            operation_id=operation_id,
            page_id=page_id,
            change=change,
            action=action,
            claims=claims,
            added_links=added_links,
            removed_links=removed_links,
        )
        write_text(
            markdown_key,
            markdown,
            "text/markdown; charset=utf-8",
        )
        write_text(
            contribution_key,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
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


def _lint_payload(
    *,
    operation_id: str,
    page_id: str,
    change: dict[str, Any],
    action: str,
    claims: list[dict[str, Any]],
    added_links: list[dict[str, Any]],
    removed_links: list[dict[str, Any]],
) -> dict[str, Any]:
    refs = list(
        dict.fromkeys(
            str(ref)
            for claim in claims
            for ref in claim.get("refs", [])
            if ref
        )
    )
    document_ids = list(
        dict.fromkeys(ref.split(":", 1)[0] for ref in refs if ":" in ref)
    )
    evidence_units = [
        {
            "evidence_id": str(claim.get("id") or f"claim-{index}"),
            "claim": str(claim.get("claim") or claim.get("text") or ""),
            "anchor_reference_ids": [
                str(ref) for ref in claim.get("refs", []) if ref
            ],
            "related_concept_slugs": [str(change["slug"])],
            "source_document_id": next(
                (
                    str(ref).split(":", 1)[0]
                    for ref in claim.get("refs", [])
                    if ":" in str(ref)
                ),
                "",
            ),
        }
        for index, claim in enumerate(claims, start=1)
    ]
    return {
        "schema_version": 1,
        "artifact_type": "lint",
        "operation_id": operation_id,
        "document_id": f"lint:{operation_id}",
        "page_id": page_id,
        "content_action": action,
        "concept": {
            "slug": str(change["slug"]),
            "title": str(change.get("title") or change["slug"]),
            "definition": str(change.get("definition") or ""),
            "source_document_ids": document_ids,
            "evidence_claim_ids": [
                str(unit["evidence_id"]) for unit in evidence_units
            ],
        },
        "evidence_units": evidence_units,
        "source_blocks": [],
        "source_key_points": [
            {
                "text": str(unit["claim"]),
                "anchor_reference_ids": unit["anchor_reference_ids"],
            }
            for unit in evidence_units
        ],
        "added_links": added_links,
        "removed_links": removed_links,
    }


def _dict_items(values: Any) -> list[dict[str, Any]]:
    return [dict(value) for value in values if isinstance(value, dict)]


def _object_key(
    workspace_id: str,
    page_id: str,
    operation_id: str,
    extension: str,
) -> str:
    return f"wiki/{workspace_id}/pages/{page_id}/ops/{operation_id}.{extension}"


def _content_hash(markdown: str) -> str:
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
