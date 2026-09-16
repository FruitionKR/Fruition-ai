from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg

from app.modules.wiki_ingestion.infrastructure.active_cluster_markdown import (
    merge_active_cluster_markdown,
)
from app.modules.wiki_ingestion.infrastructure.concept_evidence import (
    append_concept_evidence,
)
from app.modules.wiki_ingestion.infrastructure.object_storage import write_text_object
from app.modules.wiki_ingestion.infrastructure.operation_artifacts import (
    persist_operation_artifacts,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import (
    delete_source_related_links,
    load_existing_concept_ids_by_slug,
    persist_embedding_units,
    read_optional_text_object,
    resolve_or_create_wiki_page_id,
    upload_wiki_markdown,
    upsert_document_wiki_link,
    upsert_wiki_page,
    upsert_wiki_page_link,
)
from app.modules.wiki_ingestion.infrastructure.wiki_persistence_payload import (
    markdown_title,
    page_payload,
    resolve_page_id,
    source_summary,
)


def lock_concept_persistence(
    conn: psycopg.Connection,
    user_id: str,
    workspace_id: str,
) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"concept-persistence:{user_id}:{workspace_id}",),
    )


def persist_wiki_outputs(
    conn: psycopg.Connection,
    document_id: str,
    manifest: dict[str, Any],
) -> list[str]:
    normalized = _load_normalized(manifest)
    links = _load_links(manifest)
    user_id = str(manifest.get("user_id") or "local-user")
    workspace_id = str(manifest.get("workspace_id") or "local-workspace")
    operation_id = manifest.get("operation_id")
    prepared_concept_updates: list[dict[str, Any]] | None = None
    source_page_id = None
    concept_id_by_slug = None
    if operation_id:
        source_page_id = resolve_or_create_wiki_page_id(
            conn,
            user_id,
            workspace_id,
            "source",
            document_id,
        )
    source_blocks = _persist_source_blocks(conn, document_id, manifest)
    source_page_id = _persist_source_page(
        conn,
        document_id,
        manifest,
        normalized,
        user_id,
        workspace_id,
        page_id=source_page_id,
        source_blocks=source_blocks,
    )
    lock_concept_persistence(conn, user_id, workspace_id)
    if operation_id:
        concept_id_by_slug = _prepare_concept_page_ids(
            conn,
            manifest,
            normalized,
            user_id,
            workspace_id,
        )
        prepared_concept_updates = _prepare_concept_update_decisions(
            conn,
            user_id,
            workspace_id,
            (manifest.get("meaning_clusters") or {}).get(
                "concept_update_decisions",
                [],
            ),
        )
    concept_page_ids, concept_id_by_slug = _persist_concept_pages(
        conn,
        document_id,
        manifest,
        normalized,
        user_id,
        workspace_id,
        concept_id_by_slug=concept_id_by_slug,
    )
    _persist_page_links(conn, links, source_page_id, concept_id_by_slug, workspace_id)
    delete_source_related_links(conn, user_id, workspace_id)
    updated_concept_pages = _persist_meaning_cluster_artifacts(
        conn,
        document_id,
        manifest,
    ) or []
    # operation artifact(운영 로그용 markdown/JSON)는 DB 반영이 모두 끝난 뒤 마지막에 써서,
    # 이후 단계 실패로 트랜잭션이 롤백돼도 object storage에 orphan 파일이 남지 않게 한다.
    if operation_id:
        _persist_ingest_operation_artifacts(
            manifest,
            str(operation_id),
            workspace_id,
            source_page_id,
            concept_id_by_slug,
            prepared_concept_updates,
        )
    return list(
        dict.fromkeys(
            [
                source_page_id,
                *concept_page_ids,
                *(str(page["page_id"]) for page in updated_concept_pages),
            ]
        )
    )


def _persist_ingest_operation_artifacts(
    manifest: dict[str, Any],
    operation_id: str,
    workspace_id: str,
    source_page_id: str,
    concept_id_by_slug: dict[str, str],
    prepared_concept_updates: list[dict[str, Any]],
) -> None:
    source_page = page_payload(manifest["source_page"])
    concept_contributions = manifest.get("concept_contributions") or {}
    operation_concept_pages_by_slug = {}
    for page_value in manifest.get("concept_pages", []):
        page = page_payload(page_value)
        slug = str(page["slug"])
        page_id = concept_id_by_slug.get(slug)
        if page_id and slug in concept_contributions:
            operation_concept_pages_by_slug[slug] = {
                "page_id": page_id,
                "slug": slug,
                "markdown": page["markdown"],
            }
    for page in prepared_concept_updates:
        operation_concept_pages_by_slug[str(page["slug"])] = page
    manifest["operation_artifacts"] = persist_operation_artifacts(
        operation_id=operation_id,
        workspace_id=workspace_id,
        source_page_id=source_page_id,
        source_markdown=str(source_page["markdown"]),
        concept_pages=list(operation_concept_pages_by_slug.values()),
        concept_contributions=concept_contributions,
        write_text=write_text_object,
    )


def _prepare_concept_page_ids(
    conn: psycopg.Connection,
    manifest: dict[str, Any],
    normalized: dict[str, Any],
    user_id: str,
    workspace_id: str,
) -> dict[str, str]:
    concept_slugs = {
        str(page_payload(page)["slug"])
        for page in manifest.get("concept_pages", [])
    }
    concept_id_by_slug = load_existing_concept_ids_by_slug(
        conn,
        user_id,
        workspace_id,
    )
    for concept in normalized.get("concept_ledger", []):
        slug = str(concept.get("slug") or "")
        if slug in concept_slugs and slug not in concept_id_by_slug:
            concept_id_by_slug[slug] = resolve_or_create_wiki_page_id(
                conn,
                user_id,
                workspace_id,
                "concept",
                slug,
            )
    return concept_id_by_slug


def _load_normalized(manifest: dict[str, Any]) -> dict[str, Any]:
    normalized = manifest.get("normalized")
    if normalized is not None:
        return normalized
    out_dir = Path(manifest["out"])
    return json.loads((out_dir / "normalized.json").read_text(encoding="utf-8"))


def _load_links(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    links = manifest.get("links")
    if isinstance(links, str):
        links = json.loads(Path(links).read_text(encoding="utf-8"))
    return links or []


def _persist_source_page(
    conn: psycopg.Connection,
    document_id: str,
    manifest: dict[str, Any],
    normalized: dict[str, Any],
    user_id: str,
    workspace_id: str,
    *,
    page_id: str | None = None,
    source_blocks: list[dict[str, str]] | None = None,
) -> str:
    source_page_id = page_id or resolve_or_create_wiki_page_id(
        conn,
        user_id,
        workspace_id,
        "source",
        document_id,
    )
    source_page = page_payload(manifest["source_page"])
    source_markdown = source_page["markdown"]
    source_markdown_uri = upload_wiki_markdown(
        source_markdown,
        f"wiki/{user_id}/{workspace_id}/sources/{document_id}.md",
    )
    source_title = (
        source_page.get("title")
        or markdown_title(source_markdown)
        or normalized["document"].get("title")
        or document_id
    )
    upsert_wiki_page(
        conn,
        source_page_id,
        "source",
        source_title,
        document_id,
        source_summary(normalized, manifest),
        source_markdown_uri,
        user_id,
        workspace_id,
    )
    upsert_document_wiki_link(
        conn,
        document_id,
        source_page_id,
        "source_of",
        1.0,
        workspace_id,
    )
    persist_embedding_units(conn, source_page_id, document_id, source_markdown, source_blocks)
    return source_page_id


def _persist_concept_pages(
    conn: psycopg.Connection,
    document_id: str,
    manifest: dict[str, Any],
    normalized: dict[str, Any],
    user_id: str,
    workspace_id: str,
    *,
    concept_id_by_slug: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    concept_pages = [
        page_payload(page) for page in manifest.get("concept_pages", [])
    ]
    concept_pages_by_slug = {page["slug"]: page for page in concept_pages}
    generated_concept_slugs = set(concept_pages_by_slug)
    persisted_page_ids: list[str] = []
    concept_id_by_slug = concept_id_by_slug or load_existing_concept_ids_by_slug(
        conn,
        user_id,
        workspace_id,
    )
    for concept in normalized.get("concept_ledger", []):
        slug = concept["slug"]
        if slug not in generated_concept_slugs:
            continue
        existing = conn.execute(
            """
            SELECT id, title, summary, markdown_uri
            FROM wiki_pages
            WHERE user_id = %s AND workspace_id = %s
              AND page_type = 'concept' AND slug = %s AND status = 'active'
            """,
            (user_id, workspace_id, slug),
        ).fetchone()
        page_id = resolve_or_create_wiki_page_id(
            conn,
            user_id,
            workspace_id,
            "concept",
            slug,
        )
        concept_id_by_slug[slug] = page_id
        concept_page = concept_pages_by_slug[slug]
        concept_markdown = concept_page["markdown"]
        title = concept.get("title") or slug
        summary = concept.get("definition") or concept.get("why_page_worthy") or ""
        if existing:
            current_markdown = read_optional_text_object(existing["markdown_uri"])
            contribution = (manifest.get("concept_contributions") or {}).get(slug) or {}
            updates = [
                {
                    "claim_id": item.get("evidence_id"),
                    "claim": item.get("claim"),
                    "refs": item.get("anchor_reference_ids", []),
                }
                for item in contribution.get("evidence_units", [])
                if isinstance(item, dict)
            ]
            concept_markdown = (
                append_concept_evidence(current_markdown, updates)
                if current_markdown else concept_markdown
            )
            concept_page["markdown"] = concept_markdown
            title = existing.get("title") or title
            summary = existing.get("summary") or summary
        concept_markdown_uri = upload_wiki_markdown(
            concept_markdown,
            f"wiki/{user_id}/{workspace_id}/concepts/{slug}.md",
        )
        upsert_wiki_page(
            conn,
            page_id,
            "concept",
            title,
            slug,
            summary,
            concept_markdown_uri,
            user_id,
            workspace_id,
        )
        persisted_page_ids.append(page_id)
        upsert_document_wiki_link(
            conn,
            document_id,
            page_id,
            "extracted_concept",
            concept.get("importance_score"),
            workspace_id,
        )
        persist_embedding_units(conn, page_id, document_id, concept_markdown)
    return persisted_page_ids, concept_id_by_slug


def _persist_page_links(
    conn: psycopg.Connection,
    links: list[dict[str, Any]],
    source_page_id: str,
    concept_id_by_slug: dict[str, str],
    workspace_id: str,
) -> None:
    for link in links:
        from_page_id = resolve_page_id(
            link.get("source"),
            source_page_id,
            concept_id_by_slug,
        )
        to_page_id = resolve_page_id(
            link.get("target"),
            source_page_id,
            concept_id_by_slug,
        )
        if from_page_id and to_page_id and from_page_id != to_page_id:
            upsert_wiki_page_link(
                conn,
                from_page_id,
                to_page_id,
                link.get("relation") or "related_to",
                link.get("label"),
                link.get("confidence"),
                workspace_id,
            )


def _persist_source_blocks(
    conn: psycopg.Connection,
    document_id: str,
    manifest: dict[str, Any],
) -> list[dict[str, str]]:
    blocks = manifest.get("source_blocks")
    if blocks is None:
        return []
    if isinstance(blocks, str):
        path = Path(blocks)
        if not path.exists():
            return []
        blocks = json.loads(path.read_text(encoding="utf-8"))
    if blocks is None:
        return []
    normalized_blocks: dict[str, dict[str, str]] = {}
    for block in blocks:
        block_id = block.get("block_id")
        if block_id:
            normalized_blocks[str(block_id)] = {
                "block_id": str(block_id),
                "text": str(block.get("text") or ""),
            }
    conn.execute("DELETE FROM source_blocks WHERE document_id = %s", (document_id,))
    for block in normalized_blocks.values():
        conn.execute(
            """
            INSERT INTO source_blocks (document_id, block_id, text)
            VALUES (%s, %s, %s)
            ON CONFLICT (document_id, block_id) DO UPDATE SET
                text = EXCLUDED.text
            """,
            (document_id, block["block_id"], block["text"]),
        )
    return list(normalized_blocks.values())


def _persist_meaning_cluster_artifacts(
    conn: psycopg.Connection,
    document_id: str,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    artifact = manifest.get("meaning_clusters")
    if not isinstance(artifact, dict):
        return []
    user_id = str(manifest.get("user_id") or "local-user")
    workspace_id = str(manifest.get("workspace_id") or "local-workspace")
    updated_concept_pages = _apply_concept_update_decisions(
        conn,
        document_id,
        user_id,
        workspace_id,
        artifact.get("concept_update_decisions", []),
    )
    active_path = artifact.get("active_path")
    active_markdown = artifact.get("active_markdown")
    log_path = artifact.get("log_path")
    log_markdown = artifact.get("log_markdown")
    if isinstance(active_path, str) and isinstance(active_markdown, str):
        existing_active = read_optional_text_object(active_path)
        merged_active = merge_active_cluster_markdown(
            existing_active,
            active_markdown,
        )
        active_uri = write_text_object(active_path, merged_active)
        artifact["active_uri"] = active_uri
    if isinstance(log_path, str) and isinstance(log_markdown, str):
        existing_log = read_optional_text_object(log_path)
        separator = "\n" if existing_log and not existing_log.endswith("\n") else ""
        merged_log = (
            existing_log
            if log_markdown in existing_log
            else f"{existing_log}{separator}{log_markdown}"
        )
        log_uri = write_text_object(log_path, merged_log)
        artifact["log_uri"] = log_uri
    return updated_concept_pages


def _apply_concept_update_decisions(
    conn: psycopg.Connection,
    document_id: str,
    user_id: str,
    workspace_id: str,
    decisions: Any,
) -> list[dict[str, Any]]:
    prepared = _prepare_concept_update_decisions(
        conn,
        user_id,
        workspace_id,
        decisions,
    )
    _persist_prepared_concept_updates(
        conn,
        document_id,
        user_id,
        workspace_id,
        prepared,
    )
    return prepared


def _prepare_concept_update_decisions(
    conn: psycopg.Connection,
    user_id: str,
    workspace_id: str,
    decisions: Any,
) -> list[dict[str, Any]]:
    if not isinstance(decisions, list):
        return []
    by_concept: dict[str, list[dict[str, Any]]] = {}
    for decision in decisions:
        if (
            not isinstance(decision, dict)
            or decision.get("decision") != "same_concept"
        ):
            continue
        concept_slug = str(decision.get("concept_slug") or "").strip()
        claim = str(decision.get("claim") or "").strip()
        refs = [str(ref) for ref in decision.get("refs", []) if ref]
        if not concept_slug or not claim:
            continue
        by_concept.setdefault(concept_slug, []).append(
            {
                "claim_id": decision.get("claim_id"),
                "claim": claim,
                "refs": refs,
            }
        )
    changed_pages: list[dict[str, Any]] = []
    for concept_slug, updates in by_concept.items():
        row = conn.execute(
            """
            SELECT id, markdown_uri
            FROM wiki_pages
            WHERE page_type = 'concept'
              AND status = 'active'
              AND user_id = %s
              AND workspace_id = %s
              AND slug = %s
            """,
            (user_id, workspace_id, concept_slug),
        ).fetchone()
        if not row:
            continue
        markdown = read_optional_text_object(row["markdown_uri"])
        if not markdown:
            continue
        updated_markdown = append_concept_evidence(markdown, updates)
        if updated_markdown == markdown:
            continue
        changed_pages.append(
            {
                "page_id": str(row["id"]),
                "slug": concept_slug,
                "markdown": updated_markdown,
            }
        )
    return changed_pages


def _persist_prepared_concept_updates(
    conn: psycopg.Connection,
    document_id: str,
    user_id: str,
    workspace_id: str,
    changed_pages: list[dict[str, Any]],
) -> None:
    for page in changed_pages:
        current_markdown_key = (
            f"wiki/{user_id}/{workspace_id}/concepts/{page['slug']}.md"
        )
        current_markdown_uri = write_text_object(
            current_markdown_key,
            str(page["markdown"]),
        )
        conn.execute(
            """
            UPDATE wiki_pages
            SET markdown_uri = %s, updated_at = now()
            WHERE id = %s
            """,
            (current_markdown_uri, page["page_id"]),
        )
        persist_embedding_units(
            conn,
            str(page["page_id"]),
            document_id,
            str(page["markdown"]),
        )
