from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any


ArtifactWriter = Callable[[str, str, str], str]
ENTRY_LINT_FIELDS = (
    "entry_changes",
    "split_from",
    "linked_operation_ids",
    "invalidated_evidence_ids",
)


def persist_lint_operation_artifacts(
    *,
    operation_id: str,
    workspace_id: str,
    page_changes: list[dict[str, Any]],
    write_text: ArtifactWriter,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for change in page_changes:
        payload = lint_contribution(operation_id, change)
        if payload["content_action"] not in {"create", "append_evidence", "delete"} and not (
            payload["added_links"] or payload["removed_links"] or any(payload.get(key) for key in ENTRY_LINT_FIELDS)
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


def lint_contribution(operation_id: str, change: dict[str, Any]) -> dict[str, Any]:
    """페이지 변경 하나의 lint 기여 JSON. 저장하는 내용과 재생해 볼 내용이 같아야 해서 한 곳에서 만든다."""
    return _lint_payload(
        operation_id=operation_id,
        page_id=str(change["page_id"]),
        change=change,
        action=str(change.get("content_action") or "none"),
        claims=[claim for claim in change.get("claims", []) if isinstance(claim, dict)],
        added_links=_dict_items(change.get("added_links", [])),
        removed_links=_dict_items(change.get("removed_links", [])),
    )


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
            # 병합하며 덧붙인 별칭. 페이지를 기록으로 다시 만들 때도 남는다.
            "aliases": [str(alias) for alias in change.get("aliases", [])],
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
        # 이 페이지로 승격·병합하며 archived.md로 옮긴 클러스터. lint 복원이 active.md로 되돌린다.
        "archived_clusters": _dict_items(change.get("archived_clusters", [])),
        # 하위 항목 재검사(ADR 0042)가 남긴 항목 변경·분리 기록. 있을 때만 싣는다.
        **{key: change[key] for key in ENTRY_LINT_FIELDS if change.get(key)},
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
