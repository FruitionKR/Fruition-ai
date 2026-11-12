from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import psycopg

from app.core.ai_markdown_sanitizer import external_urls, sanitize_ai_markdown
from app.modules.wiki_generation.domain.concept_identity import (
    remap_concept_markdown_slugs,
    remap_normalized_concept_slugs,
)
from app.modules.wiki_generation.domain.text_utils import name_key, unique_keep_order
from app.modules.wiki_ingestion.application.concept_contribution import build_concept_contributions
from app.modules.wiki_ingestion.infrastructure.backend_document_reader import read_contributions
from app.modules.wiki_ingestion.infrastructure.pending_contributions import with_pending_contributions
from app.modules.wiki_ingestion.domain.concept_entries import (
    ENTRY_REFERENCE,
    render_entry_index,
    replay_concept_entries,
)
from app.modules.wiki_ingestion.infrastructure.active_cluster_markdown import (
    merge_active_cluster_markdown,
)
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    INVALIDATION,
    concept_title_lookup,
    load_concept_contributions,
    rebuild_concept_page,
    replay_concept_page_with_operation,
)
from app.modules.wiki_ingestion.infrastructure.concept_evidence import (
    append_concept_evidence,
    concept_evidence_updates,
)
from app.modules.wiki_ingestion.infrastructure.object_storage import read_text_object, storage_uri, write_text_object
from app.modules.wiki_ingestion.infrastructure.operation_artifacts import (
    persist_operation_artifacts,
)
from app.modules.wiki_ingestion.infrastructure.wiki_publication import (
    object_key as publication_object_key,
    write_publication_text,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import (
    delete_source_related_links,
    load_existing_concept_ids_by_slug,
    persist_embedding_units,
    read_optional_text_object,
    replace_extracted_concept_identity_aliases,
    resolve_concept_page_identity,
    resolve_or_create_wiki_page_id,
    sync_source_concept_links,
    upsert_document_wiki_link,
    stored_entry_index,
    upsert_entry_references,
    upsert_page_entries,
    upsert_concept_identity_aliases,
    upsert_wiki_page,
    upsert_wiki_page_link,
)
from app.modules.wiki_ingestion.infrastructure.wiki_persistence_payload import (
    markdown_title,
    page_payload,
    resolve_page_id,
)

# 재생 결과 페이지를 DB에 반영하는 함수(복구와 같은 `_apply_restored_wiki_state`). 저장소 모듈이 넘긴다.
ApplyRebuiltPages = Callable[..., Any]
# 빠진 하위 항목을 가리키던 참조를 정리한다(복구와 같은 `_prune_dangling_references`). 다시 그린 다른 페이지를 목록에 더한다.
# 이번에 지운 페이지와, 이번 작업이 남겼지만 아직 원장에 없는 페이지별 기여(page_id → 기여)를 함께 넘긴다.
PruneReferences = Callable[
    [psycopg.Connection, str, str, list[dict[str, Any]], list[str], dict[str, dict[str, Any]]], None
]
# 재생 결과가 빈 페이지를 내린다(복구와 같은 `_cleanup_deleted_wiki_pages`).
CleanupDeletedPages = Callable[[psycopg.Connection, str, list[str]], None]


def lock_concept_persistence(
    conn: psycopg.Connection,
    workspace_id: str,
) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"concept-persistence:{workspace_id}",),
    )


def persist_wiki_outputs(
    conn: psycopg.Connection,
    document_id: str,
    manifest: dict[str, Any],
    manual_source_markdown: str | None = None,
    apply_rebuilt_pages: ApplyRebuiltPages | None = None,
    prune_references: PruneReferences | None = None,
    cleanup_deleted_pages: CleanupDeletedPages | None = None,
    artifact_writer: Callable[[str, str, str], str] | None = None,
) -> list[str]:
    """`manual_source_markdown`은 활성 수동 기여의 source 본문이다. 있으면 재편입이 덮어쓰지 않는다.

    `apply_rebuilt_pages`를 주면 재편입이 수정·삭제한 블록의 옛 근거를 가진 개념 페이지를 기여 재생으로 다시 그린다.
    """
    normalized = _load_normalized(manifest)
    links = _load_links(manifest)
    user_id = str(manifest.get("user_id") or "local-user")
    workspace_id = str(manifest.get("workspace_id") or "local-workspace")
    operation_id = str(manifest.get("operation_id") or "")
    if not operation_id.strip():
        raise ValueError("operation_id is required for wiki publication")
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
    lock_concept_persistence(conn, workspace_id)
    concept_id_by_slug = _prepare_concept_page_ids(
        conn,
        manifest,
        normalized,
        user_id,
        workspace_id,
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
        markdown=manual_source_markdown,
        operation_id=operation_id,
    )
    # Generated pages already include their same_concept evidence in the merged contribution.
    generated_slugs = {str(page_payload(page)["slug"]) for page in manifest.get("concept_pages", [])}
    generated_slugs.intersection_update(str(concept["slug"]) for concept in normalized.get("concept_ledger", []))
    update_decisions = (manifest.get("meaning_clusters") or {}).get("concept_update_decisions", [])
    if isinstance(update_decisions, list):
        update_decisions = [decision for decision in update_decisions
                            if isinstance(decision, dict) and decision.get("concept_slug") not in generated_slugs]
    if operation_id:
        prepared_concept_updates = _prepare_concept_update_decisions(
            conn,
            workspace_id,
            update_decisions,
            document_id,
            operation_id,
        )
    concept_page_ids, concept_id_by_slug = _persist_concept_pages(
        conn,
        document_id,
        manifest,
        normalized,
        user_id,
        workspace_id,
        concept_id_by_slug=concept_id_by_slug,
        operation_id=operation_id,
    )
    _persist_page_links(conn, links, source_page_id, concept_id_by_slug, workspace_id)
    delete_source_related_links(conn, workspace_id)
    updated_concept_pages = _apply_concept_update_decisions(
        conn,
        document_id,
        workspace_id,
        operation_id,
        update_decisions,
    )
    # 기존 개념 갱신(same_concept)도 이 문서의 직접 개념 기여로 기록된다. 재생과 같게 문서 연결을 둔다(ADR 0042 결정 5).
    for page in updated_concept_pages:
        upsert_document_wiki_link(conn, document_id, str(page["page_id"]), "extracted_concept", None, workspace_id)
    rebuilt_markdown: dict[str, str] = {}
    invalidation_pages: list[dict[str, Any]] = []
    deleted_pages: list[dict[str, Any]] = []
    invalidated = _invalidated_refs(document_id, manifest)
    if operation_id and invalidated and apply_rebuilt_pages is not None:
        rebuilt_markdown, invalidation_pages, deleted_pages = _rebuild_invalidated_pages(
            conn,
            document_id,
            workspace_id,
            str(operation_id),
            invalidated,
            manifest,
            concept_id_by_slug,
            updated_concept_pages,
            apply_rebuilt_pages,
            prune_references,
            cleanup_deleted_pages,
        )
    # 모든 페이지를 쓴 뒤 문서 연결에서 자동 소스 → 개념 링크를 맞춘다. 다시 편입한 문서(휴지통에서 되살림 포함)의
    # 변경 없는 개념 연결도 소스 페이지 범위로 함께 맞춘다.
    sync_source_concept_links(
        conn,
        workspace_id,
        concept_page_ids=[
            *concept_page_ids,
            *(str(page["page_id"]) for page in updated_concept_pages),
            *rebuilt_markdown,
        ],
        source_page_ids=[source_page_id],
    )
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
            manual_source_markdown,
            rebuilt_markdown,
            invalidation_pages,
            deleted_pages,
            artifact_writer,
        )
    return list(
        dict.fromkeys(
            [
                source_page_id,
                *concept_page_ids,
                *(str(page["page_id"]) for page in updated_concept_pages),
                *rebuilt_markdown,
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
    manual_source_markdown: str | None,
    rebuilt_markdown: dict[str, str],
    invalidation_pages: list[dict[str, Any]],
    deleted_pages: list[dict[str, Any]],
    artifact_writer: Callable[[str, str, str], str] | None,
) -> None:
    if artifact_writer is None:
        artifact_writer = lambda key, text, content_type: write_publication_text(
            operation_id, key.split("/pages/", 1)[1].split("/", 1)[0], key, text, content_type
        )
    source_page = page_payload(manifest["source_page"])
    concept_contributions = {
        **(manifest.get("concept_contributions") or {}),
        **{str(page["slug"]): page["contribution"] for page in invalidation_pages},
    }
    operation_concept_pages_by_slug = {}
    for page_value in manifest.get("concept_pages", []):
        # These are final publication bytes; sanitizing again would strip preserved manual links.
        page = page_value
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
    for page in invalidation_pages:
        operation_concept_pages_by_slug[str(page["slug"])] = page
    # 다시 그린 페이지는 기록 본문도 재생 결과와 같아야 한다(document-svc가 본문 해시를 대조한다).
    operation_concept_pages_by_slug = {
        slug: {**page, "markdown": rebuilt_markdown.get(str(page["page_id"]), page["markdown"])}
        for slug, page in operation_concept_pages_by_slug.items()
    }
    manifest["operation_artifacts"] = persist_operation_artifacts(
        operation_id=operation_id,
        workspace_id=workspace_id,
        source_page_id=source_page_id,
        source_markdown=(
            manual_source_markdown
            if manual_source_markdown is not None
            else str(source_page["markdown"])
        ),
        concept_pages=list(operation_concept_pages_by_slug.values()),
        concept_contributions=concept_contributions,
        write_text=artifact_writer,
    )
    # 지운 페이지는 본문 없이 무효화 기여만 남긴다. document-svc가 이 key로 원장 행을 더해, 이 편입을 되돌리면 되살린다.
    for page in deleted_pages:
        key = publication_object_key(workspace_id, str(page["page_id"]), operation_id, "json")
        artifact_writer(
            key, json.dumps(page["contribution"], ensure_ascii=False, sort_keys=True),
            "application/json; charset=utf-8",
        )
    manifest["deleted_pages"] = [str(page["page_id"]) for page in deleted_pages]


def _invalidated_refs(document_id: str, manifest: dict[str, Any]) -> list[str]:
    changes = manifest.get("source_block_changes") or {}
    return [f"{document_id}:{block_id}" for block_id in changes.get("invalidated_block_ids", [])]


def _rebuild_invalidated_pages(
    conn: psycopg.Connection,
    document_id: str,
    workspace_id: str,
    operation_id: str,
    invalidated: list[str],
    manifest: dict[str, Any],
    concept_id_by_slug: dict[str, str],
    updated_concept_pages: list[dict[str, Any]],
    apply_rebuilt_pages: ApplyRebuiltPages,
    prune_references: PruneReferences | None,
    cleanup_deleted_pages: CleanupDeletedPages | None,
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    """재편입이 수정·삭제한 블록의 옛 근거를 가진 개념 페이지를 기여 재생으로 다시 그린다.

    이 문서와 연결된 페이지 중 활성 기여가 무효화된 블록을 인용하는 페이지만 대상이다. 이번 편입이 기여한 페이지는
    이번 기여를, 기여하지 않은 페이지는 무효화 전용 기여를 마지막에 더해 재생한다. 페이지와 다른 근거는 그대로 둔다.
    재생 결과 근거가 남지 않는 페이지는 복구와 같이 내리고 결과의 `deleted_pages`로 알린다.
    돌려주는 값: 다시 그린 페이지 본문(page_id → markdown), 무효화 전용 기여를 남길 페이지, 지운 페이지.
    """
    contributions = manifest.get("concept_contributions") or {}
    touched = {
        concept_id_by_slug[slug]: slug
        for slug in (str(page["slug"]) for page in manifest.get("concept_pages", []))
        if slug in contributions and slug in concept_id_by_slug
    }
    touched.update({str(page["page_id"]): str(page["slug"]) for page in updated_concept_pages})
    linked = conn.execute(
        """
        SELECT DISTINCT page.id, page.slug
        FROM document_wiki_links link
        JOIN wiki_pages page ON page.id = link.wiki_page_id
        WHERE link.document_id = %s AND link.relation_type = 'extracted_concept'
          AND page.page_type = 'concept' AND page.status = 'active' AND page.workspace_id = %s
        """,
        (document_id, workspace_id),
    ).fetchall()
    slugs = {**{str(row["id"]): str(row["slug"]) for row in linked}, **touched}
    if not slugs:
        return {}, [], []
    ledger = with_pending_contributions(conn, read_contributions(list(slugs), workspace_id), list(slugs))
    stale = set(invalidated)
    applied: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    invalidation_contribution = {
        "schema_version": 1,
        "artifact_type": INVALIDATION,
        "operation_id": operation_id,
        "document_id": document_id,
        "invalidated_reference_ids": invalidated,
    }
    titles = concept_title_lookup(conn, workspace_id)
    for page_id, slug in slugs.items():
        contribution = {
            **(contributions[slug] if page_id in touched else invalidation_contribution),
            "page_id": page_id,
        }
        replay = replay_concept_page_with_operation(
            workspace_id=workspace_id,
            page_id=page_id,
            operation_id=operation_id,
            ledger=ledger,
            contribution=contribution,
            read_text=read_text_object,
            load_contributions=load_concept_contributions,
            rebuild_page=partial(rebuild_concept_page, concept_titles=titles),
            required_citations=stale,
        )
        if replay is None:
            continue
        _loaded, rebuilt = replay
        if rebuilt.deleted:
            replace_extracted_concept_identity_aliases(conn, workspace_id, page_id, [])
            deleted.append({"page_id": page_id, "contribution": contribution})
            continue
        markdown_key = publication_object_key(workspace_id, page_id, operation_id)
        # 이름·요약은 편입이 기존 값을 유지하는 규칙과 같게 두고(None은 그대로 둠), 본문·항목·링크·문서 연결은 재생 결과로 맞춘다.
        page = {
            "page_id": page_id,
            "page_type": "concept",
            "markdown_key": markdown_key,
            "markdown": rebuilt.markdown,
            "title": None,
            "summary": None,
            "supported_links": list(rebuilt.supported_links),
            "source_document_ids": list(rebuilt.source_document_ids),
            "identity_aliases": list(rebuilt.identity_aliases),
            "entries": list(rebuilt.entries),
        }
        apply_rebuilt_pages(
            workspace_id, [page], {"removed_links": [], "restored_links": list(rebuilt.supported_links)}, True, conn,
        )
        applied.append(page)
    deleted_ids = [str(page["page_id"]) for page in deleted]
    if deleted_ids and cleanup_deleted_pages is not None:
        cleanup_deleted_pages(conn, workspace_id, deleted_ids)
    elif deleted_ids:
        raise RuntimeError("deleting rebuilt pages requires cleanup_deleted_pages")
    if (applied or deleted_ids) and prune_references is not None:
        # 빠진 하위 항목을 가리키던 참조를 정리한다. 그 참조를 가진 다른 페이지는 다시 그려져 목록에 더해진다.
        # 이번 편입이 기여했지만 다시 그리지 않은 페이지도 참조 정리로 다시 그려질 수 있다. 원장에는 아직 이번 기여가
        # 없으므로 함께 넘겨, 이미 쓴 이번 기여가 빠진 이전 상태로 되돌아가지 않게 한다.
        pending = {
            page_id: {**contributions[slug], "page_id": page_id}
            for page_id, slug in touched.items()
            if slug in contributions
        }
        prune_references(conn, workspace_id, operation_id, applied, deleted_ids, pending)
    rebuilt_markdown: dict[str, str] = {}
    invalidation_pages: list[dict[str, Any]] = []
    for page in applied:
        page_id = str(page["page_id"])
        markdown = str(page["markdown"])
        rebuilt_markdown[page_id] = markdown
        if page_id not in touched:
            slug = slugs.get(page_id) or str(
                conn.execute("SELECT slug FROM wiki_pages WHERE id = %s", (page_id,)).fetchone()["slug"]
            )
            invalidation_pages.append({
                "page_id": page_id,
                "slug": slug,
                "markdown": markdown,
                "contribution": {**invalidation_contribution, "page_id": page_id},
            })
    return rebuilt_markdown, invalidation_pages, deleted


def _prepare_concept_page_ids(
    conn: psycopg.Connection,
    manifest: dict[str, Any],
    normalized: dict[str, Any],
    user_id: str,
    workspace_id: str,
) -> dict[str, str]:
    pages = [page_payload(page) for page in manifest.get("concept_pages", [])]
    page_by_slug = {str(page["slug"]): page for page in pages}
    allocated: list[tuple[str, str, str]] = []
    for concept in sorted(
        normalized.get("concept_ledger", []),
        key=lambda item: (name_key(item.get("title")), str(item.get("slug") or "")),
    ):
        slug = str(concept.get("slug") or "")
        if slug not in page_by_slug:
            continue
        page_id, final_slug, _status = resolve_concept_page_identity(
            conn,
            user_id,
            workspace_id,
            title=str(concept.get("title") or page_by_slug[slug].get("title") or slug),
            identity_aliases=list(concept.get("identity_aliases") or []),
            preferred_slug=slug,
        )
        allocated.append((slug, final_slug, page_id))
    slug_map = {old: final for old, final, _page_id in allocated}
    if any(old != final for old, final in slug_map.items()):
        _remap_manifest_concept_slugs(manifest, normalized, slug_map)
    concept_id_by_slug = load_existing_concept_ids_by_slug(conn, workspace_id)
    concept_id_by_slug.update({final: page_id for _old, final, page_id in allocated})
    return concept_id_by_slug


def _remap_manifest_concept_slugs(
    manifest: dict[str, Any],
    normalized: dict[str, Any],
    slug_map: dict[str, str],
) -> None:
    def remap_ref(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if value.startswith("concept:"):
            slug = value.split(":", 1)[1]
            return f"concept:{slug_map.get(slug, slug)}"
        return slug_map.get(value, value)

    remap_normalized_concept_slugs(normalized, slug_map)
    remapped_pages = []
    for page_value in manifest.get("concept_pages", []):
        page = page_payload(page_value)
        page["slug"] = slug_map.get(page.get("slug"), page.get("slug"))
        page["markdown"] = remap_concept_markdown_slugs(str(page["markdown"]), slug_map)
        remapped_pages.append(page)
    manifest["concept_pages"] = remapped_pages
    source_page = manifest.get("source_page")
    if isinstance(source_page, dict) and source_page.get("markdown"):
        source_page["markdown"] = remap_concept_markdown_slugs(str(source_page["markdown"]), slug_map)
    contributions = manifest.get("concept_contributions") or {}
    for contribution in contributions.values():
        concept = contribution.get("concept") or {}
        concept["slug"] = slug_map.get(concept.get("slug"), concept.get("slug"))
        for evidence in contribution.get("evidence_units", []):
            evidence["related_concept_slugs"] = [
                slug_map.get(slug, slug)
                for slug in evidence.get("related_concept_slugs", [])
            ]
        for link in contribution.get("links", []):
            for field in ("source", "target"):
                if field in link:
                    link[field] = remap_ref(link[field])
    manifest["concept_contributions"] = {
        slug_map.get(slug, slug): contribution
        for slug, contribution in contributions.items()
    }
    for link in manifest.get("links", []):
        for field in ("source", "target"):
            if field in link:
                link[field] = remap_ref(link[field])
    meaning_clusters = manifest.get("meaning_clusters") or {}
    for decision in meaning_clusters.get("concept_update_decisions", []):
        if decision.get("concept_slug"):
            decision["concept_slug"] = slug_map.get(decision["concept_slug"], decision["concept_slug"])
    post_ingest = manifest.get("post_ingest") or {}
    cluster_normalized = post_ingest.get("cluster_normalized") or {}
    for concept in cluster_normalized.get("core_concepts", []):
        concept["slug"] = slug_map.get(concept.get("slug"), concept.get("slug"))
    for evidence in cluster_normalized.get("evidence_units", []):
        evidence["related_concept_slugs"] = [
            slug_map.get(slug, slug)
            for slug in evidence.get("related_concept_slugs", [])
        ]


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
    markdown: str | None = None,
    operation_id: str,
) -> str:
    source_page_id = page_id or resolve_or_create_wiki_page_id(
        conn,
        user_id,
        workspace_id,
        "source",
        document_id,
    )
    source_page = page_payload(manifest["source_page"])
    source_markdown = source_page["markdown"] if markdown is None else markdown
    source_key = publication_object_key(workspace_id, source_page_id, operation_id)
    # The operation artifact pass uploads the final coalesced bytes before this transaction commits.
    source_markdown_uri = storage_uri(source_key)
    source_title = (
        manifest.get("display_name")
        or source_page.get("title")
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
        # 소스 페이지 요약은 두지 않는다. 검색(Jev 평가셋 근거·정답 블록 적중)에서 효용이 확인되지 않았다.
        "",
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
    operation_id: str,
) -> tuple[list[str], dict[str, str]]:
    concept_pages = [
        page_payload(page) for page in manifest.get("concept_pages", [])
    ]
    manifest["concept_pages"] = concept_pages
    concept_pages_by_slug = {page["slug"]: page for page in concept_pages}
    generated_concept_slugs = set(concept_pages_by_slug)
    persisted_page_ids: list[str] = []
    if concept_id_by_slug is None:
        concept_id_by_slug = load_existing_concept_ids_by_slug(conn, workspace_id)
    for concept in normalized.get("concept_ledger", []):
        slug = concept["slug"]
        if slug not in generated_concept_slugs:
            continue
        existing = conn.execute(
            """
            SELECT id, title, summary, markdown_uri
            FROM wiki_pages
            WHERE workspace_id = %s
              AND page_type = 'concept' AND slug = %s AND status = 'active'
            """,
            (workspace_id, slug),
        ).fetchone()
        page_id = concept_id_by_slug[slug]
        concept_id_by_slug[slug] = page_id
        concept_page = concept_pages_by_slug[slug]
        concept_markdown = concept_page["markdown"]
        title = concept.get("title") or slug
        summary = concept.get("definition") or concept.get("why_page_worthy") or ""
        contribution = (manifest.get("concept_contributions") or {}).get(slug) or {}
        if existing:
            current_markdown = read_optional_text_object(existing["markdown_uri"])
            updates = concept_evidence_updates(contribution)
            concept_markdown = (
                sanitize_ai_markdown(
                    append_concept_evidence(current_markdown, updates), keep_urls=external_urls(current_markdown),
                )
                if current_markdown else concept_markdown
            )
            concept_page["markdown"] = concept_markdown
            title = existing.get("title") or title
            summary = existing.get("summary") or summary
        # 이번 편입이 둔 하위 항목·참조(ADR 0042)를 사본에 더하고, 본문 목록은 저장된 사본 전체로 다시 그린다.
        # 기여 JSON과 같은 전역 근거 형식이다. 페이지 행이 있어야 사본을 쓸 수 있어 페이지 id를 먼저 잡는다.
        entry_adds = [change for change in contribution.get("entry_changes", []) if change.get("op") == "add"]
        entry_references = [link for link in contribution.get("links", []) if link.get("relation") == ENTRY_REFERENCE]
        if entry_adds or entry_references:
            upsert_page_entries(conn, workspace_id, page_id, entry_adds)
            upsert_entry_references(conn, workspace_id, page_id, entry_references)
            stored_entries, stored_references = stored_entry_index(conn, page_id)
            concept_markdown = render_entry_index(concept_markdown, stored_entries, stored_references)
            concept_page["markdown"] = concept_markdown
        if not existing and contribution:
            # LLM output and sibling context cannot be recovered from page-local semantic inputs.
            contribution["initial_markdown"] = concept_markdown
        concept_key = publication_object_key(workspace_id, page_id, operation_id)
        concept_markdown_uri = storage_uri(concept_key)
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
        upsert_concept_identity_aliases(
            conn,
            workspace_id,
            page_id,
            list(concept.get("identity_aliases") or []),
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
    normalized_blocks: dict[str, dict[str, Any]] = {}
    for index, block in enumerate(blocks, start=1):
        block_id = block.get("block_id")
        if block_id:
            normalized_blocks[str(block_id)] = {
                "block_id": str(block_id),
                "text": str(block.get("text") or ""),
                "position": _optional_int(block.get("position")) or index,
                "line_start": _optional_int(block.get("line_start")),
                "line_end": _optional_int(block.get("line_end")),
                "block_type": block.get("block_type") or None,
            }
    conn.execute("DELETE FROM source_blocks WHERE document_id = %s", (document_id,))
    for block in normalized_blocks.values():
        conn.execute(
            """
            INSERT INTO source_blocks
                (document_id, block_id, text, position, line_start, line_end, block_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (document_id, block_id) DO UPDATE SET
                text = EXCLUDED.text,
                position = EXCLUDED.position,
                line_start = EXCLUDED.line_start,
                line_end = EXCLUDED.line_end,
                block_type = EXCLUDED.block_type
            """,
            (
                document_id,
                block["block_id"],
                block["text"],
                block["position"],
                block["line_start"],
                block["line_end"],
                block["block_type"],
            ),
        )
    _persist_source_block_snapshot(conn, document_id, manifest.get("source_content_hash"))
    return [{"block_id": block["block_id"], "text": block["text"]} for block in normalized_blocks.values()]


def _persist_source_block_snapshot(
    conn: psycopg.Connection,
    document_id: str,
    source_content_hash: object,
) -> None:
    # 블록과 같은 트랜잭션에서 갱신해, 편입이 실패하면 직전 성공 스냅샷이 그대로 남는다.
    # 해시를 모르는 블록 집합에 이전 해시가 남으면 프론트가 줄 범위를 잘못 믿으므로 지운다.
    if not source_content_hash:
        conn.execute("DELETE FROM source_block_snapshots WHERE document_id = %s", (document_id,))
        return
    conn.execute(
        """
        INSERT INTO source_block_snapshots (document_id, source_content_hash)
        VALUES (%s, %s)
        ON CONFLICT (document_id) DO UPDATE SET
            source_content_hash = EXCLUDED.source_content_hash,
            updated_at = now()
        """,
        (document_id, str(source_content_hash)),
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _persist_meaning_cluster_artifacts(manifest: dict[str, Any]) -> None:
    """의미 클러스터 파일(active.md·로그)만 쓴다. 기존 개념 갱신(same_concept)은 편입 저장이 이미 반영했다."""
    artifact = manifest.get("meaning_clusters")
    if not isinstance(artifact, dict):
        return
    workspace_id = str(manifest.get("workspace_id") or "local-workspace")
    # 저장 위치는 artifact가 아니라 워크스페이스로 정한다. 로그 파일 이름(편입 날짜)만 artifact에서 가져온다.
    active_path = f"wiki/{workspace_id}/clusters/active.md"
    active_markdown = artifact.get("active_markdown")
    artifact_log_path = artifact.get("log_path")
    log_markdown = artifact.get("log_markdown")
    if isinstance(active_markdown, str):
        existing_active = read_optional_text_object(active_path)
        merged_active = merge_active_cluster_markdown(
            existing_active,
            active_markdown,
        )
        active_uri = write_text_object(active_path, merged_active)
        artifact["active_uri"] = active_uri
    if isinstance(artifact_log_path, str) and isinstance(log_markdown, str):
        log_path = f"wiki/{workspace_id}/logs/{Path(artifact_log_path).name}"
        existing_log = read_optional_text_object(log_path)
        separator = "\n" if existing_log and not existing_log.endswith("\n") else ""
        merged_log = (
            existing_log
            if log_markdown in existing_log
            else f"{existing_log}{separator}{log_markdown}"
        )
        log_uri = write_text_object(log_path, merged_log)
        artifact["log_uri"] = log_uri


def _apply_concept_update_decisions(
    conn: psycopg.Connection,
    document_id: str,
    workspace_id: str,
    operation_id: str,
    decisions: Any,
) -> list[dict[str, Any]]:
    prepared = _prepare_concept_update_decisions(
        conn,
        workspace_id,
        decisions,
        document_id,
        operation_id,
    )
    _persist_prepared_concept_updates(
        conn,
        document_id,
        workspace_id,
        operation_id,
        prepared,
    )
    return prepared


def _prepare_concept_update_decisions(
    conn: psycopg.Connection,
    workspace_id: str,
    decisions: Any,
    document_id: str,
    operation_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(decisions, list):
        return []
    contributions = build_concept_contributions(
        operation_id=operation_id, normalized={"document": {"document_id": document_id}},
        source_blocks=[], links=[], concept_update_decisions=decisions,
    )
    changed_pages: list[dict[str, Any]] = []
    for concept_slug, contribution in contributions.items():
        updates = concept_evidence_updates(contribution)
        row = conn.execute(
            """
            SELECT id, markdown_uri
            FROM wiki_pages
            WHERE page_type = 'concept'
              AND status = 'active'
              AND workspace_id = %s
              AND slug = %s
            """,
            (workspace_id, concept_slug),
        ).fetchone()
        if not row:
            continue
        markdown = read_optional_text_object(row["markdown_uri"])
        if not markdown:
            continue
        updated_markdown = sanitize_ai_markdown(
            append_concept_evidence(markdown, updates), keep_urls=external_urls(markdown),
        )
        entry_adds = contribution.get("entry_changes", [])
        if entry_adds:
            entries, references = stored_entry_index(conn, str(row["id"]))
            entries = replay_concept_entries([
                {"entry_changes": [{"op": "add", **entry} for entry in entries]}, contribution,
            ])
            updated_markdown = render_entry_index(updated_markdown, entries, references)
        if updated_markdown == markdown and not entry_adds:
            continue
        changed_pages.append(
            {
                "page_id": str(row["id"]),
                "slug": concept_slug,
                "markdown": updated_markdown,
                **({"entry_changes": entry_adds} if entry_adds else {}),
            }
        )
    return changed_pages


def _persist_prepared_concept_updates(
    conn: psycopg.Connection,
    document_id: str,
    workspace_id: str,
    operation_id: str,
    changed_pages: list[dict[str, Any]],
) -> None:
    for page in changed_pages:
        upsert_page_entries(conn, workspace_id, str(page["page_id"]), page.get("entry_changes", []))
        current_markdown_key = publication_object_key(
            workspace_id, str(page["page_id"]), operation_id
        )
        current_markdown_uri = storage_uri(current_markdown_key)
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
