from __future__ import annotations

import json
import logging
import os
import re
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app.core.pipeline_control import PipelineRunCancelledError, task_run_id
from app.core.error_text import truncate_error
from app.modules.wiki_generation.domain.text_utils import slugify
from app.modules.wiki_ingestion.infrastructure.backend_document_reader import (
    read_contributions,
    read_document,
)
from app.modules.wiki_ingestion.infrastructure.active_cluster_markdown import (
    MATERIALIZED_CORE_RELATIONS,
    cluster_relation_items as _cluster_relation_items,
    cluster_sections_by_id as _cluster_sections_by_id,
    parse_active_cluster_lint as _parse_active_cluster_lint,
    reconcile_active_cluster_invalidations as _reconcile_active_cluster_invalidations,
    refs_in_text as _refs_in_text,
)
from app.modules.wiki_ingestion.infrastructure.concept_evidence import append_concept_evidence
from app.modules.wiki_ingestion.infrastructure.embedding_units import clean_unit_text as _clean_unit_text
from app.modules.wiki_ingestion.infrastructure.markdown_sections import (
    markdown_list_section as _markdown_list_section,
    markdown_section as _markdown_section,
)
from app.modules.wiki_ingestion.infrastructure.lint_operation_artifacts import (
    persist_lint_operation_artifacts,
)
from app.modules.wiki_ingestion.domain.orphan_link_lint import find_orphan_links
from app.modules.wiki_ingestion.infrastructure.object_storage import (
    read_text_object,
    storage_uri,
    write_text_object,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_output_persistence import (
    lock_concept_persistence as _lock_concept_persistence,
    _persist_meaning_cluster_artifacts,
    persist_wiki_outputs as _persist_wiki_outputs,
)
from app.modules.wiki_ingestion.infrastructure.source_contribution_reconciliation import (
    active_relation_keys as _active_relation_keys,
    apply_structural_reconciliation as _apply_structural_reconciliation,
    list_reconciliation_candidates as _list_reconciliation_candidates,
    _remove_stale_relations,
    source_contribution_payload as _source_contribution_payload,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import (
    delete_source_related_links as _delete_source_related_links,
    load_existing_concept_ids_by_slug as _load_existing_concept_ids_by_slug,
    persist_embedding_units as _persist_embedding_units,
    read_optional_text_object as _read_optional_text_object,
    resolve_or_create_wiki_page_id as _resolve_or_create_wiki_page_id,
    upload_wiki_markdown as _upload_wiki_markdown,
    upsert_document_wiki_link as _upsert_document_wiki_link,
    upsert_wiki_page as _upsert_wiki_page,
    upsert_wiki_page_link as _upsert_wiki_page_link,
)
from app.modules.wiki_ingestion.infrastructure.wiki_persistence_payload import (
    stored_manifest as _stored_manifest,
)
from app.modules.wiki_ingestion.infrastructure.wiki_lint_report import render_lint_log_markdown
from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import (
    concept_write_lock,
    get_concept_index,
    invalidate_concept_index,
    put_concept_index,
)


def _unique_keep_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _today_iso() -> str:
    return date.today().isoformat()


logger = logging.getLogger(__name__)


def ai_database_url() -> str:
    url = os.environ.get("AI_DATABASE_URL")
    if not url:
        raise RuntimeError("Set AI_DATABASE_URL before using ai_db-backed APIs")
    return url


def connect_ai() -> psycopg.Connection:
    """ai-svc 소유 테이블의 ai_db 연결."""
    conn = psycopg.connect(ai_database_url(), row_factory=dict_row)
    if task_run_id.get() is not None:
        try:
            conn.execute("SELECT set_config('app.ai_task_run_id', %s, true)", (task_run_id.get(),))
        except Exception:
            conn.close()
            raise
    return conn


def connect() -> psycopg.Connection:
    """Wiki·query·embedding·pipeline run의 기본 ai_db 연결."""
    return connect_ai()


def cleanup_deleted_wiki_pages(
    workspace_id: str,
    page_ids: list[str],
) -> None:
    if not page_ids:
        return
    with connect() as conn:
        _cleanup_deleted_wiki_pages(conn, workspace_id, page_ids)


def _cleanup_deleted_wiki_pages(
    conn: psycopg.Connection,
    workspace_id: str,
    page_ids: list[str],
) -> set[str]:
    if not page_ids:
        return set()
    rows = conn.execute(
        """
        SELECT id, user_id
        FROM wiki_pages
        WHERE workspace_id = %s
          AND id = ANY(%s)
        ORDER BY id
        FOR UPDATE
        """,
        (workspace_id, page_ids),
    ).fetchall()
    target_ids = [row["id"] for row in rows]
    if not target_ids:
        return set()
    user_ids = {str(row["user_id"]) for row in rows if row.get("user_id")}
    vector_rows = conn.execute(
        """
        SELECT DISTINCT embedding_vector_id
        FROM wiki_embedding_units
        WHERE page_id = ANY(%s)
        """,
        (target_ids,),
    ).fetchall()
    vector_ids = [row["embedding_vector_id"] for row in vector_rows]
    conn.execute(
        """
        DELETE FROM wiki_page_links
        WHERE from_page_id = ANY(%s)
           OR to_page_id = ANY(%s)
        """,
        (target_ids, target_ids),
    )
    conn.execute(
        "DELETE FROM document_wiki_links WHERE wiki_page_id = ANY(%s)",
        (target_ids,),
    )
    conn.execute(
        "DELETE FROM wiki_page_embeddings WHERE page_id = ANY(%s)",
        (target_ids,),
    )
    conn.execute(
        "DELETE FROM wiki_embedding_units WHERE page_id = ANY(%s)",
        (target_ids,),
    )
    if vector_ids:
        conn.execute(
            """
            DELETE FROM wiki_embedding_vectors vector
            WHERE vector.id = ANY(%s)
              AND NOT EXISTS (
                  SELECT 1
                  FROM wiki_embedding_units unit
                  WHERE unit.embedding_vector_id = vector.id
              )
            """,
            (vector_ids,),
        )
    conn.execute(
        "UPDATE wiki_pages SET status = 'deleted', updated_at = now() WHERE id = ANY(%s)",
        (target_ids,),
    )
    return user_ids


def apply_restored_wiki_state(
    workspace_id: str,
    changed_pages: list[dict[str, Any]],
    link_changes: dict[str, list[dict[str, Any]]],
    replace_links: bool,
) -> None:
    user_ids = _apply_restored_wiki_state(
        workspace_id,
        changed_pages,
        link_changes,
        replace_links,
    )
    for user_id in user_ids:
        try:
            invalidate_concept_index(user_id, workspace_id)
        except Exception:
            logger.warning("concept index cache invalidation failed", exc_info=True)


def apply_restored_wiki_state_and_cleanup(
    operation_id: str,
    workspace_id: str,
    changed_pages: list[dict[str, Any]],
    link_changes: dict[str, list[dict[str, Any]]],
    replace_links: bool,
    deleted_page_ids: list[str],
) -> None:
    if not changed_pages and not any(link_changes.values()) and not deleted_page_ids:
        return
    with concept_write_lock(workspace_id, operation_id):
        with connect() as conn:
            user_ids = _apply_restored_wiki_state(
                workspace_id,
                changed_pages,
                link_changes,
                replace_links,
                conn,
            )
            user_ids.update(
                _cleanup_deleted_wiki_pages(conn, workspace_id, deleted_page_ids)
            )
        for user_id in user_ids:
            try:
                invalidate_concept_index(user_id, workspace_id)
            except Exception:
                logger.warning("concept index cache invalidation failed", exc_info=True)


def _apply_restored_wiki_state(
    workspace_id: str,
    changed_pages: list[dict[str, Any]],
    link_changes: dict[str, list[dict[str, Any]]],
    replace_links: bool,
    conn: psycopg.Connection | None = None,
) -> set[str]:
    if not changed_pages and not any(link_changes.values()):
        return set()
    user_ids: set[str] = set()
    with (connect() if conn is None else nullcontext(conn)) as conn:
        page_ids = [str(page["page_id"]) for page in changed_pages]
        rows = conn.execute(
            """
            SELECT id, page_type, slug, user_id,
                   (
                       SELECT link.document_id
                       FROM document_wiki_links link
                       WHERE link.wiki_page_id = wiki_pages.id
                         AND link.relation_type = 'source_of'
                       ORDER BY link.created_at
                       LIMIT 1
                   ) AS source_document_id
            FROM wiki_pages
            WHERE workspace_id = %s AND id = ANY(%s)
            """,
            (workspace_id, page_ids),
        ).fetchall() if page_ids else []
        by_id = {str(row["id"]): row for row in rows}
        if len(by_id) != len(set(page_ids)):
            raise ValueError("restored Wiki page does not match workspace")
        by_ref = {
            f'{row["page_type"]}:{row["slug"]}': row
            for row in rows
        }
        target_by_ref = {
            (str(row["user_id"]), f'{row["page_type"]}:{row["slug"]}'): row
            for row in rows
        }
        source_user_ids = {str(row["user_id"]) for row in rows}
        if source_user_ids and any(link_changes.values()):
            active_rows = conn.execute(
                """
                SELECT id, page_type, slug, user_id
                FROM wiki_pages
                WHERE workspace_id = %s
                  AND user_id = ANY(%s)
                  AND status = 'active'
                """,
                (workspace_id, list(source_user_ids)),
            ).fetchall()
            for row in active_rows:
                target_by_ref.setdefault(
                    (str(row["user_id"]), f'{row["page_type"]}:{row["slug"]}'),
                    row,
                )
        for page in changed_pages:
            conn.execute(
                """
                UPDATE wiki_pages
                SET markdown_uri = %s,
                    title = COALESCE(%s, title),
                    summary = COALESCE(%s, summary),
                    status = 'active',
                    updated_at = now()
                WHERE id = %s AND workspace_id = %s
                """,
                (
                    storage_uri(str(page["markdown_key"])),
                    page.get("title"),
                    page.get("summary"),
                    page["page_id"],
                    workspace_id,
                ),
            )

            source_document_ids = page.get("source_document_ids")
            if source_document_ids is not None:
                conn.execute(
                    """
                    DELETE FROM document_wiki_links
                    WHERE wiki_page_id = %s
                      AND relation_type = 'extracted_concept'
                    """,
                    (page["page_id"],),
                )
                for document_id in dict.fromkeys(
                    str(item) for item in source_document_ids if item
                ):
                    conn.execute(
                        """
                        INSERT INTO document_wiki_links (
                            document_id, wiki_page_id, relation_type,
                            confidence, workspace_id, created_at
                        )
                        VALUES (%s, %s, 'extracted_concept', NULL, %s, now())
                        ON CONFLICT (document_id, relation_type, wiki_page_id)
                        DO UPDATE SET workspace_id = EXCLUDED.workspace_id
                        """,
                        (document_id, page["page_id"], workspace_id),
                    )

            old_vectors = conn.execute(
                """
                SELECT DISTINCT embedding_vector_id
                FROM wiki_embedding_units
                WHERE page_id = %s
                """,
                (page["page_id"],),
            ).fetchall()
            conn.execute(
                "DELETE FROM wiki_embedding_units WHERE page_id = %s",
                (page["page_id"],),
            )
            markdown = read_text_object(storage_uri(str(page["markdown_key"])))
            source_document_id = next(
                (str(item) for item in (source_document_ids or []) if item),
                None,
            )
            if (
                source_document_id is None
                and by_id[str(page["page_id"])]["page_type"] != "concept"
            ):
                restored_source_document_id = page.get("source_document_id")
                source_document_id = (
                    restored_source_document_id
                    or by_id[str(page["page_id"])].get("source_document_id")
                )
                if restored_source_document_id:
                    _upsert_document_wiki_link(
                        conn,
                        str(source_document_id),
                        str(page["page_id"]),
                        "source_of",
                        1.0,
                        workspace_id,
                    )
            if source_document_id:
                _persist_embedding_units(
                    conn,
                    str(page["page_id"]),
                    source_document_id,
                    markdown,
                )
            vector_ids = [str(row["embedding_vector_id"]) for row in old_vectors]
            if vector_ids:
                conn.execute(
                    """
                    DELETE FROM wiki_embedding_vectors vector
                    WHERE vector.id = ANY(%s)
                      AND NOT EXISTS (
                          SELECT 1 FROM wiki_embedding_units unit
                          WHERE unit.embedding_vector_id = vector.id
                      )
                    """,
                    (vector_ids,),
                )

        if replace_links and page_ids:
            conn.execute(
                "DELETE FROM wiki_page_links WHERE from_page_id = ANY(%s)",
                (page_ids,),
            )

        def resolve(reference: str, user_id: str) -> str | None:
            known = target_by_ref.get((user_id, reference))
            if known is None:
                return None
            return str(known["id"])

        for link in link_changes.get("removed_links", []):
            source = by_ref.get(str(link.get("source")))
            if source is None:
                continue
            target_id = resolve(str(link.get("target")), str(source["user_id"]))
            if target_id:
                conn.execute(
                    """
                    DELETE FROM wiki_page_links
                    WHERE from_page_id = %s AND to_page_id = %s AND link_type = %s
                    """,
                    (source["id"], target_id, link.get("relation") or "related_to"),
                )
        user_ids = {str(row["user_id"]) for row in rows}
        for link in link_changes.get("restored_links", []):
            source = by_ref.get(str(link.get("source")))
            if source is None:
                continue
            target_id = resolve(str(link.get("target")), str(source["user_id"]))
            if target_id and target_id != str(source["id"]):
                _upsert_wiki_page_link(
                    conn,
                    str(source["id"]),
                    target_id,
                    link.get("relation") or "related_to",
                    link.get("label"),
                    link.get("confidence"),
                    workspace_id,
                )
    return user_ids


REQUIRED_TABLES = (
    "wiki_pages",
    "document_wiki_links",
    "source_blocks",
    "wiki_page_links",
    "pipeline_runs",
    "wiki_page_embeddings",
    "wiki_embedding_vectors",
    "wiki_embedding_units",
)

AGENT_REQUIRED_TABLES = (
    "skills",
    "skill_versions",
    "skill_version_sources",
    "agent_runs",
    "agent_plans",
    "agent_plan_operations",
    "agent_approvals",
    "agent_jobs",
    "agent_tool_executions",
    "agent_run_artifacts",
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)

_AI_SCHEMA_SQL_PATH = Path(__file__).resolve().parents[4] / "db" / "ai_schema.sql"

# ai_db는 python이 소유한다 — db/ai_schema.sql이 원본 DDL
AI_DB_REQUIRED_TABLES = (
    "ai_task_runs",
    "ai_task_changes",
    *REQUIRED_TABLES,
    "wiki_schemas",
    "document_derived_state",
    *AGENT_REQUIRED_TABLES,
)


def verify_schema() -> None:
    """하위 호환용 ai_db 준비 상태 확인."""
    verify_ai_schema()


def verify_agent_schema() -> None:
    """Agent worker가 사용하는 Agent/Skill/checkpoint 테이블을 확인한다."""
    with connect_ai() as conn:
        rows = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = ANY(%s)
            """,
            (list(AGENT_REQUIRED_TABLES),),
        ).fetchall()
    existing_tables = {row["table_name"] for row in rows}
    missing_tables = sorted(set(AGENT_REQUIRED_TABLES) - existing_tables)
    if missing_tables:
        missing = ", ".join(missing_tables)
        raise RuntimeError(f"Agent schema is not ready; missing tables: {missing}")


def ensure_ai_schema() -> None:
    """ai_db 스키마 부트스트랩.

    AI_DB_MIGRATION_URL이 설정돼 있으면 db/ai_schema.sql(멱등 DDL)을 적용하고,
    없으면 적용은 건너뛴다. 이후 AI_DATABASE_URL로 필수 테이블 존재를 검증한다.
    api·worker 기동 경로가 공통으로 호출한다.
    """
    migration_url = os.environ.get("AI_DB_MIGRATION_URL")
    if migration_url:
        ddl = _AI_SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        with psycopg.connect(migration_url) as conn:
            conn.execute(ddl)
        logger.info("[startup] ai_db 스키마 적용 완료 (db/ai_schema.sql)")
    verify_ai_schema()


def verify_ai_schema() -> None:
    """ai_db 필수 테이블이 준비됐는지 확인한다."""
    with connect_ai() as conn:
        rows = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = ANY(%s)
            """,
            (list(AI_DB_REQUIRED_TABLES),),
        ).fetchall()
    existing_tables = {row["table_name"] for row in rows}
    missing_tables = sorted(set(AI_DB_REQUIRED_TABLES) - existing_tables)
    if missing_tables:
        missing = ", ".join(missing_tables)
        raise RuntimeError(f"ai_db schema is not ready; missing tables: {missing}")


def get_document(document_id: str) -> dict | None:
    return read_document(document_id)


def get_wiki_graph(workspace_id: str) -> dict[str, list[dict[str, Any]]]:
    with connect() as conn:
        pages = conn.execute(
            """
            SELECT page.id, page.page_type, page.title, page.slug, page.summary,
                   page.status, source_link.document_id AS source_document_id
            FROM wiki_pages page
            LEFT JOIN LATERAL (
                SELECT link.document_id
                FROM document_wiki_links link
                WHERE link.wiki_page_id = page.id
                  AND link.relation_type = 'source_of'
                ORDER BY link.created_at
                LIMIT 1
            ) source_link ON true
            WHERE page.workspace_id = %s
              AND page.status = 'active'
            ORDER BY page.created_at, page.id
            """,
            (workspace_id,),
        ).fetchall()
        links = conn.execute(
            """
            SELECT link.from_page_id, link.to_page_id, link.link_type,
                   link.label, COALESCE(link.confidence, 0.0) AS confidence
            FROM wiki_page_links link
            JOIN wiki_pages source ON source.id = link.from_page_id
            JOIN wiki_pages target ON target.id = link.to_page_id
            WHERE link.workspace_id = %s
              AND source.status = 'active'
              AND target.status = 'active'
            ORDER BY link.from_page_id, link.to_page_id, link.link_type
            """,
            (workspace_id,),
        ).fetchall()
    return {
        "nodes": [
            {
                **{key: value for key, value in dict(page).items() if key != "source_document_id"},
                "source_document": (
                    {"id": page["source_document_id"], "filename": None}
                    if page.get("source_document_id")
                    else None
                ),
            }
            for page in pages
        ],
        "edges": [dict(link) for link in links],
    }


def get_wiki_page(workspace_id: str, page_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        page = conn.execute(
            """
            SELECT id, page_type, title, slug, summary, markdown_uri, status,
                   created_at, updated_at
            FROM wiki_pages
            WHERE id = %s AND workspace_id = %s AND status = 'active'
            """,
            (page_id, workspace_id),
        ).fetchone()
        if page is None:
            return None
        source_documents = conn.execute(
            """
            SELECT document_id AS id, relation_type,
                   COALESCE(confidence, 0.0) AS confidence
            FROM document_wiki_links
            WHERE wiki_page_id = %s AND workspace_id = %s
            ORDER BY created_at
            """,
            (page_id, workspace_id),
        ).fetchall()
        related_pages = conn.execute(
            """
            SELECT target.id, target.page_type, target.title, target.slug,
                   link.link_type, link.label,
                   COALESCE(link.confidence, 0.0) AS confidence
            FROM wiki_page_links link
            JOIN wiki_pages target ON target.id = link.to_page_id
            WHERE link.from_page_id = %s
              AND link.workspace_id = %s
              AND target.status = 'active'
            ORDER BY target.title, target.id
            """,
            (page_id, workspace_id),
        ).fetchall()
    result = dict(page)
    result["markdown"] = _read_optional_text_object(page.get("markdown_uri"))
    result["source_documents"] = [
        {**dict(document), "filename": None, "source_uri": None}
        for document in source_documents
    ]
    result["related_pages"] = [dict(related) for related in related_pages]
    return result


def lookup_wiki_pages(
    page_ids: list[str],
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    if not page_ids:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, page_type, title, slug, summary, markdown_uri,
                   user_id, workspace_id, status, created_at, updated_at
            FROM wiki_pages
            WHERE id = ANY(%s)
              AND (%s::text IS NULL OR workspace_id = %s)
            ORDER BY id
            """,
            (page_ids, workspace_id, workspace_id),
        ).fetchall()
    return [dict(row) for row in rows]


def get_document_wiki_context(
    document_id: str,
    workspace_id: str,
) -> dict[str, list[dict[str, Any]]]:
    with connect() as conn:
        pages = conn.execute(
            """
            SELECT page.id, page.page_type, page.title, page.slug,
                   link.relation_type, COALESCE(link.confidence, 0.0) AS confidence
            FROM document_wiki_links link
            JOIN wiki_pages page ON page.id = link.wiki_page_id
            WHERE link.document_id = %s
              AND link.workspace_id = %s
              AND page.workspace_id = %s
              AND page.status = 'active'
            ORDER BY link.created_at, page.id
            """,
            (document_id, workspace_id, workspace_id),
        ).fetchall()
        blocks = conn.execute(
            """
            SELECT block_id, text
            FROM source_blocks
            WHERE document_id = %s
              AND EXISTS (
                  SELECT 1
                  FROM document_wiki_links link
                  WHERE link.document_id = source_blocks.document_id
                    AND link.workspace_id = %s
              )
            ORDER BY block_id
            """,
            (document_id, workspace_id),
        ).fetchall()
    return {
        "pages": [dict(page) for page in pages],
        "source_blocks": [dict(block) for block in blocks],
    }


def delete_document_wiki_data(workspace_id: str, document_id: str) -> None:
    with connect() as conn:
        scope_rows = conn.execute(
            """
            SELECT DISTINCT workspace_id
            FROM (
                SELECT workspace_id
                FROM document_wiki_links
                WHERE document_id = %s
                UNION ALL
                SELECT workspace_id
                FROM pipeline_runs
                WHERE document_id = %s
            ) document_scopes
            WHERE workspace_id IS NOT NULL
            """,
            (document_id, document_id),
        ).fetchall()
        scopes = {str(row["workspace_id"]) for row in scope_rows}
        if not scopes:
            return
        if scopes != {workspace_id}:
            raise ValueError("document Wiki state does not match workspace")
        rows = conn.execute(
            """
            SELECT wiki_page_id
            FROM document_wiki_links
            WHERE document_id = %s
              AND workspace_id = %s
              AND relation_type = 'source_of'
            """,
            (document_id, workspace_id),
        ).fetchall()
    cleanup_deleted_wiki_pages(
        workspace_id,
        [str(row["wiki_page_id"]) for row in rows],
    )
    with connect() as conn:
        conn.execute(
            "DELETE FROM document_wiki_links WHERE document_id = %s AND workspace_id = %s",
            (document_id, workspace_id),
        )
        conn.execute("DELETE FROM source_blocks WHERE document_id = %s", (document_id,))
        conn.execute(
            """
            UPDATE pipeline_runs
            SET status = 'failed', error = 'source document deleted',
                updated_at = now(), finished_at = now()
            WHERE document_id = %s
              AND workspace_id = %s
              AND status = 'running'
            """,
            (document_id, workspace_id),
        )


def get_last_wiki_updated_at(workspace_id: str) -> Any | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(updated_at) AS updated_at FROM wiki_pages WHERE workspace_id = %s",
            (workspace_id,),
        ).fetchone()
    return row.get("updated_at") if row else None


def rename_wiki_page(
    page_id: str,
    user_id: str,
    workspace_id: str,
    title: str,
    update_slug: bool,
) -> dict[str, Any] | None:
    with connect() as conn:
        page = conn.execute(
            """
            SELECT id, page_type, title, slug
            FROM wiki_pages
            WHERE id = %s AND user_id = %s AND workspace_id = %s
              AND status = 'active'
            FOR UPDATE
            """,
            (page_id, user_id, workspace_id),
        ).fetchone()
        if page is None:
            return None
        slug = slugify(title) if update_slug else str(page["slug"])
        updated = conn.execute(
            """
            UPDATE wiki_pages
            SET title = %s, slug = %s, updated_at = now()
            WHERE id = %s
            RETURNING updated_at
            """,
            (title, slug, page_id),
        ).fetchone()
    return {
        "id": page_id,
        "page_type": page["page_type"],
        "title": title,
        "previous_title": page["title"],
        "slug": slug,
        "previous_slug": page["slug"],
        "slug_updated": update_slug,
        "updated_at": updated["updated_at"],
    }


def create_pipeline_run(
    run_id: str,
    document_id: str | None,
    user_id: str | None,
    workspace_id: str | None,
    input_source: str,
    output_dir: str,
    mode: str,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pipeline_runs (
                id, document_id, user_id, workspace_id,
                input_source, output_dir, mode, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'running')
            ON CONFLICT (id) DO NOTHING
            """,
            (
                run_id,
                document_id,
                user_id,
                workspace_id,
                input_source,
                output_dir,
                mode,
            ),
        )


def finish_pipeline_run(
    run_id: str,
    manifest: dict[str, Any],
    expected_source_hash: str | None = None,
) -> list[str]:
    embedded_page_ids: list[str] = []
    with connect() as conn:
        row = conn.execute(
            "SELECT document_id, user_id, workspace_id FROM pipeline_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
    document_id = row["document_id"] if row else None
    user_id = row["user_id"] if row else None
    workspace_id = row["workspace_id"] if row else None
    lock = concept_write_lock(str(workspace_id), run_id) if document_id else nullcontext()
    with lock:
        with connect() as conn:
            active = conn.execute(
                "SELECT status FROM pipeline_runs WHERE id = %s FOR UPDATE", (run_id,),
            ).fetchone()
            if active is None or active["status"] != "running":
                raise PipelineRunCancelledError("Pipeline run no longer accepts outputs.")
            if document_id:
                embedded_page_ids = _persist_wiki_outputs(conn, document_id, manifest)
                post_ingest = manifest.get("post_ingest")
                if isinstance(post_ingest, dict):
                    post_ingest["page_ids"] = embedded_page_ids
                manifest["source_contribution"] = _source_contribution_payload(manifest)
                manifest = _stored_manifest(manifest)
            conn.execute(
                """
                UPDATE pipeline_runs
                SET status = 'succeeded', manifest = %s,
                    updated_at = now(), finished_at = now()
                WHERE id = %s
                """,
                (Json(manifest), run_id),
            )
    if document_id:
        try:
            invalidate_concept_index(str(user_id), str(workspace_id))
        except Exception:
            logger.warning("concept index cache invalidation failed", exc_info=True)
    # core 트랜잭션 커밋 후 ai_db 파생 추적을 갱신한다.
    # 파생 추적은 best-effort — 원자성 비대상. 실패해도 ingest 결과에 영향 없다.
    if document_id:
        _mark_derived_state_ingested(document_id, expected_source_hash)
    return embedded_page_ids


def _mark_derived_state_ingested(
    document_id: str,
    expected_source_hash: str | None,
) -> None:
    if expected_source_hash is None:
        return
    try:
        with connect_ai() as conn:
            conn.execute(
                """
                UPDATE document_derived_state
                SET ingested_hash = %s,
                    last_ingested_at = now(),
                    updated_at = now()
                WHERE document_id = %s
                  AND last_edit_hash = %s
                """,
                (expected_source_hash, document_id, expected_source_hash),
            )
    except Exception:
        logger.warning(
            "[derived-state] ingest 완료 반영 실패 (best-effort, 무시) document_id=%s",
            document_id,
            exc_info=True,
        )


def fail_pipeline_run(run_id: str, error: str) -> None:
    error_message = truncate_error(error)
    with connect() as conn:
        conn.execute(
            """
            UPDATE pipeline_runs
            SET status = 'failed', error = %s,
                updated_at = now(), finished_at = now()
            WHERE id = %s AND status = 'running'
            """,
            (error_message, run_id),
        )


def checkpoint_pipeline_run(run_id: str, manifest: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE pipeline_runs
            SET manifest = %s, updated_at = now()
            WHERE id = %s AND status = 'running'
            """,
            (Json(manifest), run_id),
        )


def update_pipeline_run_post_ingest(
    run_id: str,
    values: dict[str, Any],
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE pipeline_runs
            SET manifest = jsonb_set(
                    coalesce(manifest, '{}'::jsonb),
                    '{post_ingest}',
                    coalesce(manifest->'post_ingest', '{}'::jsonb) || %s::jsonb
                ),
                updated_at = now()
            WHERE id = %s
            """,
            (Json(values), run_id),
        )


def update_pipeline_run_cluster_contribution(
    run_id: str,
    artifact: dict[str, Any],
) -> None:
    contribution = _source_contribution_payload(
        {"meaning_clusters": artifact}
    )
    values = {
        "claim_signatures": contribution["claim_signatures"],
        "relation_signatures": contribution["relation_signatures"],
    }
    with connect() as conn:
        conn.execute(
            """
            UPDATE pipeline_runs
            SET manifest = jsonb_set(
                    manifest,
                    '{source_contribution}',
                    coalesce(manifest->'source_contribution', '{}'::jsonb)
                        || %s::jsonb
                ),
                updated_at = now()
            WHERE id = %s AND status = 'succeeded'
            """,
            (Json(values), run_id),
        )


def persist_post_ingest_clusters(
    document_id: str,
    user_id: str,
    workspace_id: str,
    artifact: dict[str, Any],
) -> list[str]:
    manifest = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "meaning_clusters": artifact,
    }
    with concept_write_lock(workspace_id, f"post-ingest:{document_id}"):
        with connect() as conn:
            changed_pages = _persist_meaning_cluster_artifacts(
                conn,
                document_id,
                manifest,
            )
    if changed_pages:
        invalidate_concept_index(user_id, workspace_id)
    return [str(page["page_id"]) for page in changed_pages]


def touch_pipeline_run(run_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE pipeline_runs pr
            SET updated_at = now()
            WHERE pr.id = %s
              AND pr.status = 'running'
            RETURNING pr.id
            """,
            (run_id,),
        ).fetchone()
    return row is not None


def get_pipeline_run(run_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, document_id, user_id, workspace_id,
                   input_source, output_dir, mode, status,
                   manifest, error, created_at, updated_at, finished_at
            FROM pipeline_runs
            WHERE id = %s
            """,
            (run_id,),
        ).fetchone()
        return dict(row) if row else None


def list_active_concept_index(user_id: str = "local-user", workspace_id: str = "local-workspace") -> list[dict[str, Any]]:
    try:
        cached = get_concept_index(user_id, workspace_id)
        if cached is not None:
            return cached
    except Exception:
        logger.warning("concept index cache read failed", exc_info=True)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT slug, title, summary, markdown_uri
            FROM wiki_pages
            WHERE page_type = 'concept'
              AND status = 'active'
              AND user_id = %s
              AND workspace_id = %s
            ORDER BY slug
            """,
            (user_id, workspace_id),
        ).fetchall()
    concepts = []
    for row in rows:
        markdown = _read_optional_text_object(row["markdown_uri"])
        concepts.append(_concept_index_from_markdown(row["slug"], row["title"], row["markdown_uri"], markdown))
    try:
        put_concept_index(user_id, workspace_id, concepts)
    except Exception:
        logger.warning("concept index cache write failed", exc_info=True)
    return concepts


def latest_source_extraction_artifact(document_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT manifest
            FROM pipeline_runs
            WHERE document_id = %s
              AND status = 'succeeded'
              AND manifest ? 'source_extraction_artifact'
            ORDER BY finished_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()
    if not row:
        return None
    manifest = row.get("manifest") or {}
    artifact = manifest.get("source_extraction_artifact")
    return artifact if isinstance(artifact, dict) else None


def latest_source_page_context(
    document_id: str,
    user_id: str = "local-user",
    workspace_id: str = "local-workspace",
) -> dict[str, Any] | None:
    artifact = latest_source_extraction_artifact(document_id)
    if artifact is None:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT markdown_uri
            FROM wiki_pages
            WHERE page_type = 'source'
              AND slug = %s
              AND status = 'active'
              AND user_id = %s
              AND workspace_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (slugify(document_id), user_id, workspace_id),
        ).fetchone()
    if not row:
        return None
    source_markdown = _read_optional_text_object(row["markdown_uri"])
    return {
        "artifact": artifact,
        "source_markdown": source_markdown,
    }


def list_source_blocks(document_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT document_id, block_id, text
            FROM source_blocks
            WHERE document_id = %s
            ORDER BY block_id
            """,
            (document_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_source_blocks_for_refs(refs: list[str]) -> list[dict[str, str]]:
    with connect() as conn:
        blocks = _source_blocks_for_refs(conn, refs)
    return [
        {
            "source_document_id": ref.partition(":")[0],
            "block_id": ref.partition(":")[2],
            "text": str(block.get("text") or ""),
        }
        for block in blocks
        if (ref := str(block.get("ref") or "")) and ":" in ref
    ]


def _concept_index_from_markdown(slug: str, title: str, markdown_uri: str, markdown: str) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": title,
        "aliases": _markdown_list_section(markdown, "Aliases"),
        "definition": _markdown_section(markdown, "Definition"),
        "evidence": _markdown_list_section(markdown, "Evidence"),
        "why_page_worthy": _markdown_section(markdown, "Why It Matters"),
        "path": markdown_uri,
    }


PromotionPageGenerator = Callable[[dict[str, Any]], dict[str, Any]]


def _cluster_reconciliation_state(
    candidates: list[dict[str, Any]],
) -> tuple[
    set[str],
    set[tuple[str, str, str]],
    set[tuple[str, str, str, tuple[str, ...]]],
]:
    source_refs = {
        ref
        for candidate in candidates
        if candidate.get("_cluster_reconciliation_ready", False)
        for ref in candidate["invalidated_source_refs"]
    }
    claim_signatures = {
        tuple(signature)
        for candidate in candidates
        for signature in candidate["_current_claim_signatures"]
    }
    relation_signatures = {
        (
            str(signature[0]),
            str(signature[1]),
            str(signature[2]),
            tuple(str(item) for item in signature[3]),
        )
        for candidate in candidates
        for signature in candidate["_current_relation_signatures"]
    }
    return source_refs, claim_signatures, relation_signatures


def lint_wiki_workspace(
    user_id: str = "local-user",
    workspace_id: str = "local-workspace",
    *,
    materialize_promotions: bool = False,
    promotion_page_generator: PromotionPageGenerator | None = None,
    apply_reconciliation: bool = False,
    operation_id: str | None = None,
    write_log: bool = True,
    connection: psycopg.Connection | None = None,
) -> dict[str, Any]:
    active_path = f"wiki/{user_id}/{workspace_id}/clusters/active.md"
    lint_date = _today_iso()
    log_path = f"wiki/{user_id}/{workspace_id}/logs/{lint_date}.md"
    active_markdown = _read_optional_text_object(active_path)
    with _connection_scope(connection) as conn:
        reconciliation_candidates = _list_reconciliation_candidates(
            conn,
            user_id,
            workspace_id,
        )
    stale_source_refs = {
        ref
        for candidate in reconciliation_candidates
        for ref in candidate["invalidated_source_refs"]
    }
    (
        cluster_reconciliation_source_refs,
        current_claim_signatures,
        current_relation_signatures,
    ) = _cluster_reconciliation_state(reconciliation_candidates)
    applied_cluster_reconciliation = {
        "removed_claims": [],
        "removed_relations": [],
    }
    pending_active_markdown: str | None = None
    pending_archived_sections: list[str] = []
    if apply_reconciliation and not (
        materialize_promotions and promotion_page_generator is not None
    ):
        (
            reconciled_markdown,
            removed_claims,
            removed_relations,
        ) = _reconcile_active_cluster_invalidations(
            active_markdown,
            cluster_reconciliation_source_refs,
            current_claim_signatures,
            current_relation_signatures,
        )
        if reconciled_markdown != active_markdown:
            active_markdown = reconciled_markdown
            pending_active_markdown = reconciled_markdown
        applied_cluster_reconciliation = {
            "removed_claims": removed_claims,
            "removed_relations": removed_relations,
        }
    clusters = _parse_active_cluster_lint(active_markdown)
    source_refs = _unique_keep_order(
        ref
        for cluster in clusters
        for ref in cluster.get("refs", [])
    )
    orphan_refs = _orphan_source_refs(source_refs)
    invalid_relations = [
        {
            "cluster_id": cluster["id"],
            **relation,
        }
        for cluster in clusters
        for relation in cluster.get("invalid_relations", [])
    ]
    invalid_promotions = [
        {
            "cluster_id": cluster["id"],
            "reason": "promotion candidate has no source_refs",
        }
        for cluster in clusters
        if cluster.get("promotion_status") == "candidate" and not cluster.get("promotion_source_refs")
    ]
    blocked_cluster_ids = {
        cluster["id"]
        for cluster in clusters
        for claim in cluster.get("claims", [])
        if stale_source_refs.intersection(claim.get("refs", []))
        and (
            cluster["id"],
            str(claim.get("id") or ""),
            str(claim.get("text") or ""),
        )
        not in current_claim_signatures
    }
    promotion_candidates = [
        cluster["id"]
        for cluster in clusters
        if cluster.get("promotion_status") == "candidate" and cluster.get("promotion_source_refs")
        and cluster["id"] not in blocked_cluster_ids
    ]
    needs_review = [
        cluster["id"]
        for cluster in clusters
        if cluster.get("promotion_status") == "needs_review"
        or any(claim.get("decision") == "needs_review" for claim in cluster.get("claims", []))
        or cluster["id"] in blocked_cluster_ids
    ]
    relation_candidates = [
        {
            "cluster_id": cluster["id"],
            "target": relation.get("target"),
            "relation": relation.get("relation"),
            "evidence": relation.get("evidence", []),
        }
        for cluster in clusters
        for relation in cluster.get("relations", [])
    ]
    result = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "active_path": active_path,
        "cluster_count": len(clusters),
        "source_ref_count": len(source_refs),
        "orphan_refs": orphan_refs,
        "promotion_candidates": promotion_candidates,
        "needs_review": needs_review,
        "relation_candidates": relation_candidates,
        "invalid_relations": invalid_relations,
        "invalid_promotions": invalid_promotions,
        "reconciliation_candidates": [
            {
                key: value
                for key, value in candidate.items()
                if key not in {"user_id", "workspace_id"}
                and not key.startswith("_")
            }
            for candidate in reconciliation_candidates
        ],
        "applied_reconciliations": [],
        "applied_cluster_reconciliation": applied_cluster_reconciliation,
        "materialized_promotions": [],
        "merged_promotions": [],
        "materialized_relations": [],
    }
    if apply_reconciliation and not (
        materialize_promotions and promotion_page_generator is not None
    ):
        with _connection_scope(connection) as conn:
            result["applied_reconciliations"] = _apply_structural_reconciliation(
                conn,
                reconciliation_candidates,
                _active_relation_keys(conn, user_id, workspace_id),
            )
    if materialize_promotions and promotion_page_generator is not None:
        with _connection_scope(connection) as conn:
            materialized = _materialize_promotion_candidates(
                conn,
                user_id,
                workspace_id,
                [
                    cluster
                    for cluster in clusters
                    if cluster["id"] not in blocked_cluster_ids
                ],
                active_markdown,
                promotion_page_generator,
                operation_id=operation_id,
                apply_reconciliation=apply_reconciliation,
            )
        result["materialized_promotions"] = materialized["promotions"]
        result["merged_promotions"] = materialized["merged_promotions"]
        result["materialized_relations"] = materialized["relations"]
        result["_lint_page_changes"] = materialized["page_changes"]
        pending_active_markdown = materialized["active_markdown"]
        pending_archived_sections = materialized["archived_sections"]
        if apply_reconciliation:
            result["applied_reconciliations"] = materialized[
                "applied_reconciliations"
            ]
            result["applied_cluster_reconciliation"] = {
                "removed_claims": materialized["removed_claims"],
                "removed_relations": materialized["removed_relations"],
            }
        result["active_path"] = active_path
    if pending_active_markdown is not None or pending_archived_sections:
        result["_lint_object_changes"] = {
            "active_path": active_path,
            "active_markdown": pending_active_markdown,
            "archived_sections": pending_archived_sections,
        }
    if write_log:
        write_wiki_lint_log(result, lint_date)
    return result


def lint_orphan_wiki_links(
    user_id: str,
    workspace_id: str,
    *,
    apply: bool,
    connection: psycopg.Connection | None = None,
) -> dict[str, list[dict[str, Any]]]:
    with _connection_scope(connection) as conn:
        # 워크스페이스 전체 lint 이력을 무제한으로 읽으면 이력이 쌓일수록 dry-run
        # 미리보기까지 선형으로 느려진다. managed_link_keys 판정은 각 링크의 source
        # 페이지 자신의 기여 로그만 보므로(리소스 관리 원칙: 기여는 항상 그 페이지가
        # source인 링크만 기록한다), 지금 살아 있고(status='active') 현재
        # wiki_page_links에서 실제로 링크를 source하고 있는 페이지로만 좁혀도
        # find_orphan_links의 판정 결과는 그대로 유지된다.
        contribution_page_rows = conn.execute(
            """
            SELECT page.id
            FROM wiki_pages page
            WHERE page.page_type = 'concept'
              AND page.status = 'active'
              AND page.user_id = %s
              AND page.workspace_id = %s
              AND EXISTS (
                  SELECT 1
                  FROM wiki_page_links link
                  WHERE link.from_page_id = page.id
              )
            """,
            (user_id, workspace_id),
        ).fetchall()
        contribution_rows = read_contributions(
            [str(row["id"]) for row in contribution_page_rows],
            workspace_id,
        )
        link_rows = conn.execute(
            """
            SELECT concat(source_page.page_type, ':', source_page.slug) AS source,
                   concat(target_page.page_type, ':', target_page.slug) AS target,
                   link.link_type AS relation,
                   source_page.status AS source_status,
                   target_page.status AS target_status
            FROM wiki_page_links link
            JOIN wiki_pages source_page ON source_page.id = link.from_page_id
            JOIN wiki_pages target_page ON target_page.id = link.to_page_id
            WHERE source_page.user_id = %s
              AND source_page.workspace_id = %s
              AND target_page.user_id = %s
              AND target_page.workspace_id = %s
            """,
            (user_id, workspace_id, user_id, workspace_id),
        ).fetchall()

        managed_contributions = []
        active_contributions = []
        for row in contribution_rows:
            payload = json.loads(read_text_object(str(row["object_key"])))
            if not isinstance(payload, dict):
                raise RuntimeError(
                    "concept contribution log must be a JSON object"
                )
            managed_contributions.append(payload)
            if bool(row["active"]):
                active_contributions.append(payload)

        current_links = [
            {
                "source": str(row["source"]),
                "target": str(row["target"]),
                "relation": str(row["relation"]),
            }
            for row in link_rows
        ]
        deleted_page_refs = {
            str(row[key])
            for row in link_rows
            for key, status_key in (
                ("source", "source_status"),
                ("target", "target_status"),
            )
            if str(row[status_key]) != "active"
        }
        candidates = find_orphan_links(
            current_links=current_links,
            active_contribution_json=active_contributions,
            managed_contribution_json=managed_contributions,
            deleted_page_refs=deleted_page_refs,
        )
        removed = (
            _remove_stale_relations(
                conn,
                candidates,
                set(),
                user_id,
                workspace_id,
            )
            if apply
            else []
        )
    return {
        "orphan_link_candidates": candidates,
        "removed_orphan_links": removed,
    }


def write_wiki_lint_log(
    result: dict[str, Any],
    lint_date: str | None = None,
) -> str:
    date_value = lint_date or _today_iso()
    log_path = (
        f"wiki/{result['user_id']}/{result['workspace_id']}/logs/"
        f"{date_value}.md"
    )
    existing_log = _read_optional_text_object(log_path)
    separator = "\n" if existing_log and not existing_log.endswith("\n") else ""
    write_text_object(
        log_path,
        f"{existing_log}{separator}{render_lint_log_markdown(result, date_value)}",
    )
    return log_path


def persist_lint_operation_result(
    user_id: str,
    workspace_id: str,
    operation_id: str,
    result: dict[str, Any],
    connection: psycopg.Connection | None = None,
) -> list[dict[str, Any]]:
    page_changes = {
        str(change["slug"]): dict(change)
        for change in result.pop("_lint_page_changes", [])
    }
    added_links = [
        _normalized_relation_link(item)
        for item in result.get("materialized_relations", [])
    ]
    removed_links = [
        _normalized_relation_link(item)
        for reconciliation in result.get("applied_reconciliations", [])
        for item in reconciliation.get("removed_relations", [])
    ]
    removed_links.extend(
        _normalized_relation_link(item)
        for item in result.get("removed_orphan_links", [])
    )
    links_by_source: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for field, links in (
        ("added_links", added_links),
        ("removed_links", removed_links),
    ):
        for link in links:
            source = str(link.get("source") or "")
            if not source.startswith("concept:"):
                continue
            slug = source.split(":", 1)[1]
            links_by_source.setdefault(
                slug,
                {"added_links": [], "removed_links": []},
            )[field].append(link)

    missing_slugs = [slug for slug in links_by_source if slug not in page_changes]
    if missing_slugs:
        with _connection_scope(connection) as conn:
            rows = conn.execute(
                """
                SELECT id, slug, title, summary, markdown_uri
                FROM wiki_pages
                WHERE page_type = 'concept'
                  AND user_id = %s
                  AND workspace_id = %s
                  AND slug = ANY(%s)
                """,
                (user_id, workspace_id, missing_slugs),
            ).fetchall()
        for row in rows:
            # _read_optional_text_object는 읽기 실패도 빈 문자열로 삼켜서, 저장소 장애를
            # 진짜 빈 페이지와 구분하지 못한 채 그대로 operation artifact로 저장해버린다.
            # 여기서는 실패를 그대로 전파해 빈 markdown이 정상 산출물로 저장되지 않게 한다.
            markdown = read_text_object(str(row["markdown_uri"]))
            page_changes[str(row["slug"])] = {
                "page_id": str(row["id"]),
                "slug": str(row["slug"]),
                "title": str(row["title"]),
                "definition": str(row.get("summary") or ""),
                "markdown": markdown,
                "content_action": "none",
                "claims": [],
            }

    for slug, link_actions in links_by_source.items():
        change = page_changes.get(slug)
        if change is None:
            raise RuntimeError(
                f"failed to resolve lint operation source page: concept:{slug}"
            )
        change.update(link_actions)

    return persist_lint_operation_artifacts(
        operation_id=operation_id,
        workspace_id=workspace_id,
        page_changes=list(page_changes.values()),
        write_text=write_text_object,
    )


def apply_lint_object_changes(result: dict[str, Any]) -> list[str]:
    changes = result.pop("_lint_object_changes", None)
    if not isinstance(changes, dict):
        return []
    written_keys: list[str] = []
    active_markdown = changes.get("active_markdown")
    if active_markdown is not None:
        active_path = str(changes["active_path"])
        write_text_object(active_path, str(active_markdown))
        written_keys.append(active_path)
    archive_path = _append_archived_clusters(
        str(result["user_id"]),
        str(result["workspace_id"]),
        list(changes.get("archived_sections") or []),
    )
    if archive_path:
        written_keys.append(archive_path)
    return written_keys


def _connection_scope(connection: psycopg.Connection | None):
    return nullcontext(connection) if connection is not None else connect()


def _normalized_relation_link(item: dict[str, Any]) -> dict[str, Any]:
    source = str(item.get("source") or item.get("from") or "")
    target = str(item.get("target") or item.get("to") or "")
    return {
        "source": source if ":" in source else f"concept:{source}",
        "target": target if ":" in target else f"concept:{target}",
        "relation": str(item.get("relation") or "related_to"),
    }


def _orphan_source_refs(refs: list[str]) -> list[str]:
    if not refs:
        return []
    existing = set()
    by_document: dict[str, list[str]] = {}
    for ref in refs:
        document_id, _sep, block_id = ref.partition(":")
        if document_id and block_id:
            by_document.setdefault(document_id, []).append(block_id)
    with connect() as conn:
        for document_id, block_ids in by_document.items():
            rows = conn.execute(
                """
                SELECT block_id
                FROM source_blocks
                WHERE document_id = %s
                  AND block_id = ANY(%s)
                """,
                (document_id, block_ids),
            ).fetchall()
            existing.update(f"{document_id}:{row['block_id']}" for row in rows)
    return [ref for ref in refs if ref not in existing]


def _materialize_promotion_candidates(
    conn: psycopg.Connection,
    user_id: str,
    workspace_id: str,
    clusters: list[dict[str, Any]],
    active_markdown: str,
    promotion_page_generator: PromotionPageGenerator,
    *,
    operation_id: str | None = None,
    apply_reconciliation: bool = False,
) -> dict[str, Any]:
    archived_sections: list[str] = []
    materialized_promotions: list[dict[str, Any]] = []
    merged_promotions: list[dict[str, Any]] = []
    page_changes: list[dict[str, Any]] = []
    existing_concept_ids = _load_existing_concept_ids_by_slug(conn, user_id, workspace_id)
    eligible_cluster_ids = {cluster["id"] for cluster in clusters}
    prepared_pages: dict[str, dict[str, Any]] = {}
    for cluster in clusters:
        cluster_id = cluster["id"]
        if cluster.get("promotion_status") != "candidate":
            continue
        if not cluster.get("promotion_source_refs"):
            continue
        claims = [claim for claim in cluster.get("claims", []) if claim.get("refs")]
        if not claims or cluster_id in existing_concept_ids:
            continue
        source_refs = _unique_keep_order(ref for claim in claims for ref in claim.get("refs", []))
        prepared_pages[cluster_id] = promotion_page_generator(
            {
                **cluster,
                "claims": claims,
                "source_blocks": _source_blocks_for_refs(conn, source_refs),
            }
        )

    _lock_concept_persistence(conn, user_id, workspace_id)
    applied_reconciliations: list[dict[str, Any]] = []
    reconciliation_source_refs: set[str] = set()
    current_claim_signatures: set[tuple[str, str, str]] = set()
    current_relation_signatures: set[
        tuple[str, str, str, tuple[str, ...]]
    ] = set()
    if apply_reconciliation:
        reconciliation_candidates = _list_reconciliation_candidates(
            conn,
            user_id,
            workspace_id,
        )
        (
            reconciliation_source_refs,
            current_claim_signatures,
            current_relation_signatures,
        ) = _cluster_reconciliation_state(reconciliation_candidates)
        applied_reconciliations = _apply_structural_reconciliation(
            conn,
            reconciliation_candidates,
            _active_relation_keys(conn, user_id, workspace_id),
        )
    latest_active_markdown = _read_optional_text_object(
        f"wiki/{user_id}/{workspace_id}/clusters/active.md"
    ) or ""
    removed_claims: list[dict[str, Any]] = []
    removed_relations: list[dict[str, Any]] = []
    active_markdown_changed = False
    if apply_reconciliation:
        reconciled_markdown, removed_claims, removed_relations = (
            _reconcile_active_cluster_invalidations(
                latest_active_markdown,
                reconciliation_source_refs,
                current_claim_signatures,
                current_relation_signatures,
            )
        )
        active_markdown_changed = reconciled_markdown != latest_active_markdown
        latest_active_markdown = reconciled_markdown
    clusters = [
        cluster
        for cluster in _parse_active_cluster_lint(latest_active_markdown)
        if cluster["id"] in eligible_cluster_ids
    ]
    active_sections = _cluster_sections_by_id(latest_active_markdown)
    existing_concept_ids = _load_existing_concept_ids_by_slug(conn, user_id, workspace_id)
    for cluster in clusters:
        cluster_id = cluster["id"]
        if cluster.get("promotion_status") != "candidate":
            continue
        if not cluster.get("promotion_source_refs"):
            continue
        claims = [claim for claim in cluster.get("claims", []) if claim.get("refs")]
        if not claims:
            continue
        if cluster_id in existing_concept_ids:
            merged_page = _merge_promotion_into_existing_concept(
                conn,
                user_id,
                workspace_id,
                cluster_id,
                claims,
                operation_id=operation_id,
            )
            if merged_page is not None:
                merged_promotions.append(
                    {
                        "cluster_id": cluster_id,
                        "concept_slug": cluster_id,
                        "page_id": existing_concept_ids[cluster_id],
                    }
                )
                page_changes.append(merged_page)
                section = active_sections.pop(cluster_id, "")
                if section:
                    archived_sections.append(
                        f"{section}\n\n### Archived\nmerged_to: concept:{cluster_id}\nmerged_at: {_today_iso()}"
                    )
            continue
        page = prepared_pages.get(cluster_id)
        if page is None:
            continue
        source_refs = _unique_keep_order(ref for claim in claims for ref in claim.get("refs", []))
        markdown = append_concept_evidence(
            str(page.get("markdown") or "").strip(),
            [
                {
                    "claim_id": claim.get("id"),
                    "claim": claim.get("claim") or claim.get("text"),
                    "refs": claim.get("refs", []),
                }
                for claim in claims
            ],
        ).strip()
        title = str(page.get("title") or cluster_id).strip()
        slug = slugify(str(page.get("slug") or cluster_id))
        if not markdown or not slug or slug == "untitled":
            continue
        page_id = _resolve_or_create_wiki_page_id(conn, user_id, workspace_id, "concept", slug)
        if operation_id:
            markdown_uri = storage_uri(
                f"wiki/{workspace_id}/pages/{page_id}/ops/{operation_id}.md"
            )
        else:
            markdown_uri = _upload_wiki_markdown(
                markdown + "\n",
                f"wiki/{user_id}/{workspace_id}/concepts/{slug}.md",
            )
        _upsert_wiki_page(conn, page_id, "concept", title, slug, _markdown_section(markdown, "Definition"), markdown_uri, user_id, workspace_id)
        first_document_id = source_refs[0].split(":", 1)[0] if source_refs else ""
        _persist_embedding_units(conn, page_id, first_document_id, markdown)
        existing_concept_ids[slug] = page_id
        materialized_promotions.append({"cluster_id": cluster_id, "concept_slug": slug, "page_id": page_id})
        page_changes.append(
            {
                "page_id": page_id,
                "slug": slug,
                "title": title,
                "definition": _markdown_section(markdown, "Definition"),
                "markdown": markdown + "\n",
                "content_action": "create",
                "claims": claims,
            }
        )
        section = active_sections.pop(cluster_id, "")
        if section:
            archived_sections.append(f"{section}\n\n### Archived\npromoted_to: concept:{slug}\npromoted_at: {_today_iso()}")
    # 관계 재료화는 promotion이 모두 끝나 existing_concept_ids가 완성된 뒤 한 곳에서만 수행한다(근거 검증·dedup 포함).
    materialized_relations = _materialize_active_relation_candidates(conn, clusters, existing_concept_ids, workspace_id)
    active_markdown_update = latest_active_markdown if active_markdown_changed else None
    if materialized_promotions or merged_promotions:
        lines = ["# Active Meaning Clusters"]
        for cluster_id in sorted(active_sections):
            lines.append(active_sections[cluster_id].strip())
        active_markdown_update = "\n\n".join(lines).rstrip() + "\n"
    return {
        "promotions": materialized_promotions,
        "merged_promotions": merged_promotions,
        "relations": materialized_relations,
        "page_changes": page_changes,
        "active_markdown": active_markdown_update,
        "archived_sections": archived_sections,
        "applied_reconciliations": applied_reconciliations,
        "removed_claims": removed_claims,
        "removed_relations": removed_relations,
    }


def _merge_promotion_into_existing_concept(
    conn: psycopg.Connection,
    user_id: str,
    workspace_id: str,
    concept_slug: str,
    claims: list[dict[str, Any]],
    *,
    operation_id: str | None = None,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, title, summary, markdown_uri
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
        return None
    markdown = _read_optional_text_object(row["markdown_uri"])
    if not markdown:
        return None
    updates = [
        {
            "claim_id": claim.get("id"),
            "claim": claim.get("claim") or claim.get("text"),
            "refs": claim.get("refs", []),
        }
        for claim in claims
    ]
    updated_markdown = append_concept_evidence(markdown, updates)
    if operation_id:
        current_markdown_uri = storage_uri(
            f"wiki/{workspace_id}/pages/{row['id']}/ops/{operation_id}.md"
        )
    else:
        current_markdown_key = (
            f"wiki/{user_id}/{workspace_id}/concepts/{concept_slug}.md"
        )
        current_markdown_uri = write_text_object(
            current_markdown_key,
            updated_markdown,
        )
    _upsert_wiki_page(
        conn,
        str(row["id"]),
        "concept",
        str(row.get("title") or concept_slug),
        concept_slug,
        str(row.get("summary") or ""),
        current_markdown_uri,
        user_id,
        workspace_id,
    )
    if updated_markdown != markdown:
        first_ref = next(
            (ref for claim in claims for ref in claim.get("refs", [])),
            "",
        )
        document_id = first_ref.split(":", 1)[0] if first_ref else ""
        _persist_embedding_units(conn, row["id"], document_id, updated_markdown)
    return {
        "page_id": str(row["id"]),
        "slug": concept_slug,
        "title": str(row.get("title") or concept_slug),
        "definition": str(row.get("summary") or ""),
        "markdown": updated_markdown,
        "content_action": "append_evidence",
        "claims": claims,
    }


def _materialize_active_relation_candidates(
    conn: psycopg.Connection,
    clusters: list[dict[str, Any]],
    concept_ids_by_slug: dict[str, str],
    workspace_id: str,
) -> list[dict[str, Any]]:
    materialized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for cluster in clusters:
        source_slug = str(cluster.get("id") or "")
        source_page_id = concept_ids_by_slug.get(source_slug)
        if not source_page_id:
            continue
        for relation in cluster.get("relations", []):
            relation_type = str(relation.get("relation") or "")
            if relation_type not in MATERIALIZED_CORE_RELATIONS:
                continue
            target_slug = str(relation.get("target") or "").split("concept:", 1)[-1]
            target_page_id = concept_ids_by_slug.get(target_slug)
            if not target_page_id or target_page_id == source_page_id:
                continue
            evidence_refs = _relation_evidence_source_refs(cluster, relation)
            if not evidence_refs:
                continue
            key = (source_slug, target_slug, relation_type)
            if key in seen:
                continue
            seen.add(key)
            _upsert_wiki_page_link(conn, source_page_id, target_page_id, relation_type, relation.get("reason"), 0.8, workspace_id)
            materialized.append(
                {
                    "from": source_slug,
                    "to": target_slug,
                    "relation": relation_type,
                    "evidence": relation.get("evidence", []),
                    "source_refs": evidence_refs,
                }
            )
    return materialized


def _relation_evidence_source_refs(cluster: dict[str, Any], relation: dict[str, Any]) -> list[str]:
    claims_by_id = {str(claim.get("id") or ""): claim for claim in cluster.get("claims", [])}
    refs: list[str] = []
    for item in relation.get("evidence", []):
        evidence = str(item).strip()
        if not evidence:
            continue
        if re.search(r"[A-Za-z0-9_.-]+:B\d{4}", evidence):
            refs.append(evidence)
            continue
        claim = claims_by_id.get(evidence)
        if claim:
            refs.extend(str(ref) for ref in claim.get("refs", []) if ref)
    return _unique_keep_order(refs)


def _source_blocks_for_refs(conn: psycopg.Connection, refs: list[str]) -> list[dict[str, str]]:
    if not refs:
        return []
    by_document: dict[str, list[str]] = {}
    for ref in refs:
        document_id, _sep, block_id = ref.partition(":")
        if document_id and block_id:
            by_document.setdefault(document_id, []).append(block_id)
    rows_out: list[dict[str, str]] = []
    for document_id, block_ids in by_document.items():
        rows = conn.execute(
            """
            SELECT block_id, text
            FROM source_blocks
            WHERE document_id = %s
              AND block_id = ANY(%s)
            ORDER BY block_id
            """,
            (document_id, block_ids),
        ).fetchall()
        for row in rows:
            rows_out.append({"ref": f"{document_id}:{row['block_id']}", "text": row["text"]})
    return rows_out


def _append_archived_clusters(
    user_id: str, workspace_id: str, sections: list[str]
) -> str | None:
    if not sections:
        return None
    archive_path = f"wiki/{user_id}/{workspace_id}/clusters/archived.md"
    existing = _read_optional_text_object(archive_path)
    separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
    write_text_object(archive_path, f"{existing}{separator}" + "\n\n".join(sections).rstrip() + "\n")
    return archive_path
