from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg

from app.modules.wiki_ingestion.infrastructure.active_cluster_markdown import (
    parse_active_cluster_lint,
)


def list_reconciliation_candidates(
    conn: psycopg.Connection,
    user_id: str,
    workspace_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH scoped_runs AS (
            SELECT run.id AS pipeline_run_id,
                   run.document_id,
                   run.user_id,
                   run.workspace_id,
                   run.manifest,
                   row_number() OVER (
                       PARTITION BY run.document_id
                       ORDER BY run.finished_at DESC NULLS LAST,
                                run.created_at DESC,
                                run.id DESC
                   ) AS run_rank
            FROM pipeline_runs run
            WHERE run.status = 'succeeded'
              AND run.manifest IS NOT NULL
              AND run.user_id = %s
              AND run.workspace_id = %s
        )
        SELECT current_run.pipeline_run_id,
               current_run.document_id,
               current_run.user_id,
               current_run.workspace_id,
               current_run.manifest,
               COALESCE(previous.manifests, '[]'::jsonb) AS previous_manifests,
               COALESCE(linked_concepts.slugs, ARRAY[]::text[]) AS linked_concept_slugs
        FROM scoped_runs current_run
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(previous_run.manifest ORDER BY previous_run.run_rank DESC)
                   AS manifests
            FROM scoped_runs previous_run
            WHERE previous_run.document_id = current_run.document_id
              AND previous_run.run_rank > 1
        ) previous ON true
        LEFT JOIN LATERAL (
            SELECT array_agg(page.slug ORDER BY page.slug) AS slugs
            FROM document_wiki_links link
            JOIN wiki_pages page ON page.id = link.wiki_page_id
            WHERE link.document_id = current_run.document_id
              AND link.relation_type = 'extracted_concept'
              AND page.page_type = 'concept'
        ) linked_concepts ON true
        WHERE current_run.run_rank = 1
          AND jsonb_array_length(
              COALESCE(
                  current_run.manifest->'source_contribution'
                      ->'source_block_changes'->'invalidated_block_ids',
                  current_run.manifest
                      ->'source_block_changes'->'invalidated_block_ids',
                  '[]'::jsonb
              )
          ) > 0
        ORDER BY current_run.document_id
        """,
        (user_id, workspace_id),
    ).fetchall()
    return [_reconciliation_candidate(row) for row in rows]


def apply_structural_reconciliation(
    conn: psycopg.Connection,
    candidates: list[dict[str, Any]],
    active_relation_keys: set[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["structural_reconciled"]:
            continue
        removed_concepts, removed_embedding_units = _remove_stale_document_concepts(
            conn,
            candidate["document_id"],
            candidate["stale_concept_slugs"],
        )
        removed_relations = _remove_stale_relations(
            conn,
            candidate["stale_relations"],
            active_relation_keys,
            candidate["user_id"],
            candidate["workspace_id"],
        )
        conn.execute(
            """
            UPDATE pipeline_runs
            SET manifest = jsonb_set(
                manifest,
                '{source_contribution}',
                COALESCE(
                    manifest->'source_contribution',
                    '{}'::jsonb
                ) || jsonb_build_object(
                    'structural_reconciled_at',
                    now()
                ),
                true
            )
            WHERE id = %s
            """,
            (candidate["pipeline_run_id"],),
        )
        applied.append(
            {
                "pipeline_run_id": candidate["pipeline_run_id"],
                "document_id": candidate["document_id"],
                "removed_concept_slugs": removed_concepts,
                "removed_embedding_unit_count": removed_embedding_units,
                "removed_relations": removed_relations,
            }
        )
    return applied


def active_relation_keys(
    conn: psycopg.Connection,
    user_id: str,
    workspace_id: str,
) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (run.document_id)
               run.manifest
        FROM pipeline_runs run
        WHERE run.status = 'succeeded'
          AND run.manifest IS NOT NULL
          AND run.user_id = %s
          AND run.workspace_id = %s
        ORDER BY run.document_id,
                 run.finished_at DESC NULLS LAST,
                 run.created_at DESC,
                 run.id DESC
        """,
        (user_id, workspace_id),
    ).fetchall()
    return {
        _relation_key(relation)
        for row in rows
        for relation in _relations(
            _contribution_from_manifest(row.get("manifest") or {})
        )
    }


def source_contribution_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    concept_slugs = []
    for page in manifest.get("concept_pages", []):
        if isinstance(page, dict) and page.get("slug"):
            concept_slugs.append(str(page["slug"]))
        elif isinstance(page, (str, Path)):
            concept_slugs.append(Path(page).stem)
    meaning_clusters = manifest.get("meaning_clusters")
    active_cluster_markdown = (
        meaning_clusters.get("active_markdown", "")
        if isinstance(meaning_clusters, dict)
        else ""
    )
    return {
        "concept_slugs": list(dict.fromkeys(concept_slugs)),
        "links": _manifest_links(manifest),
        "source_block_changes": manifest.get("source_block_changes") or {},
        "claim_signatures": _claim_signatures(active_cluster_markdown),
        "relation_signatures": _relation_signatures(active_cluster_markdown),
    }


def _contribution_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    contribution = manifest.get("source_contribution")
    if isinstance(contribution, dict):
        return contribution
    return source_contribution_payload(manifest)


def _manifest_links(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    links = manifest.get("links")
    if isinstance(links, (str, Path)):
        path = Path(links)
        if not path.exists():
            return []
        links = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(links, list):
        return []
    return [
        {
            "source": str(link.get("source") or ""),
            "target": str(link.get("target") or ""),
            "relation": str(link.get("relation") or "related_to"),
        }
        for link in links
        if isinstance(link, dict)
    ]


def _reconciliation_candidate(row: dict[str, Any]) -> dict[str, Any]:
    manifest = row.get("manifest") or {}
    stored_contribution = manifest.get("source_contribution")
    payload = _contribution_from_manifest(manifest)
    previous_payloads = [
        _contribution_from_manifest(previous_manifest)
        for previous_manifest in row.get("previous_manifests") or []
        if isinstance(previous_manifest, dict)
    ]
    document_id = str(row["document_id"])
    invalidated_ids = (
        payload.get("source_block_changes", {}).get("invalidated_block_ids", [])
    )
    current_concepts = set(payload.get("concept_slugs", []))
    previous_concepts = set(row.get("linked_concept_slugs", []))
    current_relation_keys = {_relation_key(item) for item in _relations(payload)}
    stale_relations_by_key = {
        _relation_key(item): item
        for previous_payload in previous_payloads
        if isinstance(previous_payload, dict)
        for item in _relations(previous_payload)
        if _relation_key(item) not in current_relation_keys
    }
    return {
        "pipeline_run_id": str(row["pipeline_run_id"]),
        "document_id": document_id,
        "user_id": str(row["user_id"]),
        "workspace_id": str(row["workspace_id"]),
        "invalidated_source_refs": [
            f"{document_id}:{block_id}" for block_id in invalidated_ids
        ],
        "stale_concept_slugs": sorted(previous_concepts - current_concepts),
        "stale_relations": list(stale_relations_by_key.values()),
        "structural_reconciled": (
            payload.get("structural_reconciled_at") is not None
        ),
        "_cluster_reconciliation_ready": (
            isinstance(stored_contribution, dict)
            and isinstance(stored_contribution.get("claim_signatures"), list)
            and isinstance(stored_contribution.get("relation_signatures"), list)
        ),
        "_current_claim_signatures": payload.get("claim_signatures", []),
        "_current_relation_signatures": payload.get("relation_signatures", []),
    }


def _relations(payload: dict[str, Any]) -> list[dict[str, str]]:
    links = payload.get("links", [])
    return [item for item in links if isinstance(item, dict)]


def _relation_key(relation: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(relation.get("source") or ""),
        str(relation.get("target") or ""),
        str(relation.get("relation") or "related_to"),
    )


def _claim_signatures(markdown: Any) -> list[list[str]]:
    if not isinstance(markdown, str):
        return []
    return [
        [cluster["id"], str(claim.get("id") or ""), str(claim.get("text") or "")]
        for cluster in parse_active_cluster_lint(markdown)
        for claim in cluster.get("claims", [])
    ]


def _relation_signatures(markdown: Any) -> list[list[Any]]:
    if not isinstance(markdown, str):
        return []
    return [
        [
            cluster["id"],
            str(relation.get("target") or ""),
            str(relation.get("relation") or ""),
            [str(item) for item in relation.get("evidence", [])],
        ]
        for cluster in parse_active_cluster_lint(markdown)
        for relation in cluster.get("relations", [])
    ]


def _remove_stale_document_concepts(
    conn: psycopg.Connection,
    document_id: str,
    concept_slugs: list[str],
) -> tuple[list[str], int]:
    if not concept_slugs:
        return [], 0
    rows = conn.execute(
        """
        DELETE FROM document_wiki_links link
        USING wiki_pages page
        WHERE link.document_id = %s
          AND link.relation_type = 'extracted_concept'
          AND page.id = link.wiki_page_id
          AND page.page_type = 'concept'
          AND page.slug = ANY(%s)
        RETURNING page.id, page.slug
        """,
        (document_id, concept_slugs),
    ).fetchall()
    page_ids = [str(row["id"]) for row in rows]
    removed_embedding_units = 0
    if page_ids:
        result = conn.execute(
            """
            DELETE FROM wiki_embedding_units
            WHERE source_document_id = %s
              AND page_id = ANY(%s)
            """,
            (document_id, page_ids),
        )
        removed_embedding_units = result.rowcount
    return (
        sorted(str(row["slug"]) for row in rows),
        removed_embedding_units,
    )


def _remove_stale_relations(
    conn: psycopg.Connection,
    relations: list[dict[str, str]],
    active_keys: set[tuple[str, str, str]],
    user_id: str,
    workspace_id: str,
) -> list[dict[str, str]]:
    removed: list[dict[str, str]] = []
    for relation in relations:
        if _relation_key(relation) in active_keys:
            continue
        row = conn.execute(
            """
            DELETE FROM wiki_page_links link
            USING wiki_pages source_page, wiki_pages target_page
            WHERE source_page.id = link.from_page_id
              AND target_page.id = link.to_page_id
              AND source_page.page_type = split_part(%s, ':', 1)
              AND source_page.slug = split_part(%s, ':', 2)
              AND target_page.page_type = split_part(%s, ':', 1)
              AND target_page.slug = split_part(%s, ':', 2)
              AND link.link_type = %s
              AND source_page.user_id = %s
              AND source_page.workspace_id = %s
              AND target_page.user_id = %s
              AND target_page.workspace_id = %s
            RETURNING link.from_page_id
            """,
            (
                relation["source"],
                relation["source"],
                relation["target"],
                relation["target"],
                relation["relation"],
                user_id,
                workspace_id,
                user_id,
                workspace_id,
            ),
        ).fetchone()
        if row:
            removed.append(relation)
    return removed
