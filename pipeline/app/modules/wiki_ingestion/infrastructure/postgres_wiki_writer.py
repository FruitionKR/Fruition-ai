from __future__ import annotations

import os
import uuid

import psycopg
from minio.error import S3Error

from app.modules.wiki_generation.domain.text_utils import name_key, slugify
from app.modules.wiki_generation.infrastructure.ref_format import ref_label

from app.modules.wiki_ingestion.infrastructure.embedding_units import (
    dedupe_units,
    extract_embedding_units,
    hash_text,
)
from app.modules.wiki_ingestion.infrastructure.object_storage import (
    read_text_object,
    write_text_object,
)


def persist_embedding_units(
    conn: psycopg.Connection,
    page_id: str,
    document_id: str,
    markdown: str,
    source_blocks: list[dict[str, str]] | None = None,
) -> None:
    units = extract_embedding_units(markdown) if markdown else []
    units.extend(
        {
            "unit_type": "source_block",
            "block_refs": [block["block_id"]],
            "text": block["text"],
            "weight": 1.0,
        }
        for block in source_blocks or []
        if block.get("block_id") and block.get("text")
    )
    units = dedupe_units(units)
    # 하위 항목 단위와 그 항목에 딸린 근거 단위(ADR 0042). 참조는 집 페이지 항목이 아니라 벡터를 두지 않는다.
    entries = conn.execute(
        """
        SELECT entry.entry_id, entry.name, entry.definition, entry.block_refs, entry.aliases,
               COALESCE(parent.name, page.title, page.slug) AS parent_name
        FROM wiki_page_entries entry
        JOIN wiki_pages page ON page.id = entry.page_id
        LEFT JOIN wiki_page_entries parent
          ON parent.workspace_id = entry.workspace_id
         AND parent.entry_id = entry.parent_entry_id
         AND parent.page_id = entry.page_id
        WHERE entry.page_id = %s
        ORDER BY entry.entry_id
        """,
        (page_id,),
    ).fetchall()
    for unit in units:
        unit["entry_id"] = _unit_entry_id(unit["block_refs"], entries)
    units.extend(
        {
            "unit_type": "sub_entry",
            "block_refs": list(entry["block_refs"] or []),
            # 표현 텍스트는 "바로 위 이름 > 이름"과 별칭·정의다. 근거가 늘어도 바뀌지 않아 벡터를 다시 쓴다.
            "text": _sub_entry_text(entry),
            "weight": 1.0,
            "entry_id": entry["entry_id"],
        }
        for entry in entries
    )
    previous_vectors = conn.execute(
        """
        SELECT DISTINCT embedding_vector_id
        FROM wiki_embedding_units
        WHERE page_id = %s
        """,
        (page_id,),
    ).fetchall()
    conn.execute("DELETE FROM wiki_embedding_units WHERE page_id = %s", (page_id,))
    embedding_model = os.environ.get("EMBEDDING_MODEL_NAME") or "BAAI/bge-m3"
    for unit in units:
        representation_text = unit["text"].strip()
        representation_hash = hash_text(representation_text)
        vector_identity = f"{embedding_model}:{representation_hash}"
        vector_id = f"embedding:{hash_text(vector_identity)}"
        unit_id = f"unit:{hash_text('|'.join([page_id, unit['unit_type'], ','.join(unit['block_refs']), unit['text']]))[:24]}"
        vector_row = conn.execute(
            """
            INSERT INTO wiki_embedding_vectors (
                id,
                embedding_model,
                representation_hash,
                representation_text,
                status,
                updated_at
            )
            VALUES (%s, %s, %s, %s, 'pending', now())
            ON CONFLICT (embedding_model, representation_hash) DO UPDATE SET
                representation_text = EXCLUDED.representation_text,
                updated_at = now()
            RETURNING id
            """,
            (
                vector_id,
                embedding_model,
                representation_hash,
                representation_text,
            ),
        ).fetchone()
        vector_id = str(vector_row["id"])
        conn.execute(
            """
            INSERT INTO wiki_embedding_units (
                id,
                embedding_vector_id,
                page_id,
                source_document_id,
                unit_type,
                block_refs,
                text,
                weight,
                entry_id,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                embedding_vector_id = EXCLUDED.embedding_vector_id,
                page_id = EXCLUDED.page_id,
                source_document_id = EXCLUDED.source_document_id,
                unit_type = EXCLUDED.unit_type,
                block_refs = EXCLUDED.block_refs,
                text = EXCLUDED.text,
                weight = EXCLUDED.weight,
                entry_id = EXCLUDED.entry_id,
                updated_at = now()
            """,
            (
                unit_id,
                vector_id,
                page_id,
                document_id,
                unit["unit_type"],
                unit["block_refs"],
                unit["text"],
                unit["weight"],
                unit["entry_id"],
            ),
        )
    previous_vector_ids = [row["embedding_vector_id"] for row in previous_vectors]
    if previous_vector_ids:
        conn.execute(
            """
            DELETE FROM wiki_embedding_vectors vector
            WHERE vector.id = ANY(%s)
              AND NOT EXISTS (
                  SELECT 1 FROM wiki_embedding_units unit
                  WHERE unit.embedding_vector_id = vector.id
              )
            """,
            (previous_vector_ids,),
        )


def _unit_entry_id(block_refs: list[str], entries: list[dict]) -> str | None:
    """근거 단위의 블록이 모두 한 하위 항목의 근거 블록이면 그 항목. 없거나 여러 항목에 걸리면 페이지 단위다."""
    # ponytail: 블록으로 근거를 항목에 붙인다. 상위 항목과 같은 블록을 쓰는 근거도 항목에 붙는다. 근거 id로 잇는 것은 4단계 검색 평가로 정한다.
    refs = {_ref_key(ref) for ref in block_refs}
    owners = [entry["entry_id"] for entry in entries if refs and refs <= {_ref_key(ref) for ref in entry["block_refs"] or []}]
    return owners[0] if len(owners) == 1 else None


def _ref_key(ref: str) -> str:
    # 본문은 블록을 B0001처럼 짧게 표시한다(display_ref).
    document_id, _, block_id = str(ref).rpartition(":")
    return f"{document_id}:{ref_label(block_id)}"


def _sub_entry_text(entry) -> str:
    # 하위 항목이 된 개념은 페이지 벡터가 없어 별칭으로도 이 단위가 찾혀야 한다. 이름과 같은 별칭은 뺀다.
    aliases = [alias for alias in entry["aliases"] or [] if name_key(alias) != name_key(entry["name"])]
    return (f"{entry['parent_name']} > {entry['name']}" + (f" ({', '.join(aliases)})" if aliases else "")
            + (f": {entry['definition']}" if entry["definition"] else ""))


def upsert_page_entries(
    conn: psycopg.Connection,
    workspace_id: str,
    page_id: str,
    entry_adds: list[dict],
) -> None:
    """편입이 둔 하위 항목을 조회용 사본에 더한다. 있는 항목은 재생처럼 이름·정의·위치를 두고 근거 블록만 더한다."""
    for change in entry_adds:
        conn.execute(
            """
            INSERT INTO wiki_page_entries
                (workspace_id, entry_id, page_id, name, parent_entry_id, definition, block_refs, slugs, aliases, tags)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (workspace_id, entry_id) DO UPDATE SET
                block_refs = ARRAY(
                    SELECT DISTINCT ref
                    FROM unnest(wiki_page_entries.block_refs || EXCLUDED.block_refs) AS ref
                    ORDER BY ref
                ),
                -- 재생처럼 원래 slug·별칭·태그는 기여마다 더한다.
                slugs = ARRAY(SELECT DISTINCT value FROM unnest(wiki_page_entries.slugs || EXCLUDED.slugs) AS value ORDER BY value),
                aliases = ARRAY(SELECT DISTINCT value FROM unnest(wiki_page_entries.aliases || EXCLUDED.aliases) AS value ORDER BY value),
                tags = ARRAY(SELECT DISTINCT value FROM unnest(wiki_page_entries.tags || EXCLUDED.tags) AS value ORDER BY value)
            """,
            (
                workspace_id,
                change["entry_id"],
                page_id,
                change.get("name") or "",
                change.get("parent_entry_id"),
                change.get("definition") or "",
                list(change.get("anchor_reference_ids") or []),
                [change["slug"]] if change.get("slug") else [],
                list(change.get("aliases") or []),
                list(change.get("tags") or []),
            ),
        )


def upsert_entry_references(
    conn: psycopg.Connection,
    workspace_id: str,
    page_id: str,
    references: list[dict],
) -> None:
    """참조 사본을 쓴다. 같은 항목 참조를 다시 두면 재생처럼 위치는 두고 내용만 바꾼다."""
    for link in references:
        conn.execute(
            """
            INSERT INTO wiki_page_entry_references
                (page_id, entry_id, workspace_id, target_slug, name, parent_entry_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (page_id, entry_id, parent_entry_id) DO UPDATE SET
                target_slug = EXCLUDED.target_slug,
                name = EXCLUDED.name
            """,
            (
                page_id,
                link["entry_id"],
                workspace_id,
                str(link.get("target") or "").removeprefix("concept:"),
                link.get("name") or link["entry_id"],
                link.get("parent_entry_id"),
            ),
        )


def stored_entry_index(conn: psycopg.Connection, page_id: str) -> tuple[list[dict], list[dict]]:
    """본문 목록을 그릴 저장된 항목과 참조. render_entry_index 입력 형식이다."""
    entries = conn.execute(
        "SELECT entry_id, name, parent_entry_id FROM wiki_page_entries WHERE page_id = %s ORDER BY position",
        (page_id,),
    ).fetchall()
    references = conn.execute(
        """
        SELECT entry_id, 'concept:' || target_slug AS target, name, parent_entry_id,
               'entry_reference' AS relation
        FROM wiki_page_entry_references
        WHERE page_id = %s
        ORDER BY position
        """,
        (page_id,),
    ).fetchall()
    return [dict(row) for row in entries], [dict(row) for row in references]


def upload_wiki_markdown(markdown: str, object_name: str) -> str:
    return write_text_object(object_name, markdown)


def read_optional_text_object(object_name: str) -> str:
    try:
        return read_text_object(object_name)
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject"}:
            return ""
        raise


SOURCE_MENTIONS_CONCEPT = "source_mentions_concept"


def sync_source_concept_links(
    conn: psycopg.Connection,
    workspace_id: str,
    *,
    concept_page_ids: list[str] = (),
    source_page_ids: list[str] = (),
    document_ids: list[str] = (),
) -> None:
    """자동 소스 → 개념 링크를 문서 연결에서 다시 만든다(ADR 0042 결정 5). 모든 페이지 최종 상태를 쓴 뒤 부른다.

    소스 페이지(문서 D) → 개념 P 링크는 P의 문서 연결(`extracted_concept`, 재생 결과의 유효 근거 문서)에 D가 있을 때만
    있다. 소스·개념이 활성이고 D가 휴지통(tombstone)에 없어야 한다. 사람이 소스 본문에 쓴 `[[slug]]`는
    `wiki_source_manual_links`로 따로 더하고, 수동 삭제는 자동 링크보다 우선한다. 범위는 주어진 개념 페이지로 오는
    링크와 주어진 소스 페이지(문서)에서 나가는 링크다. 같은 트랜잭션에서 문서 연결을 바꾼 뒤 부른다.
    """
    source_page_ids = [str(item) for item in source_page_ids]
    if document_ids:
        source_page_ids += [
            str(row["wiki_page_id"])
            for row in conn.execute(
                """
                SELECT wiki_page_id FROM document_wiki_links
                WHERE workspace_id = %s AND relation_type = 'source_of' AND document_id = ANY(%s)
                """,
                (workspace_id, [str(item) for item in document_ids]),
            ).fetchall()
        ]
    scope = {"ws": workspace_id, "pages": [str(item) for item in concept_page_ids], "sources": source_page_ids}
    if not scope["pages"] and not scope["sources"]:
        return
    conn.execute(
        """
        DELETE FROM wiki_page_links
        WHERE workspace_id = %(ws)s AND link_type = 'source_mentions_concept'
          AND (to_page_id = ANY(%(pages)s) OR from_page_id = ANY(%(sources)s))
        """,
        scope,
    )
    conn.execute(
        """
        INSERT INTO wiki_page_links (
            from_page_id, to_page_id, link_type, label, confidence, workspace_id, created_at, updated_at
        )
        SELECT source.id, concept.id, 'source_mentions_concept', NULL, NULL, source.workspace_id, now(), now()
        FROM wiki_pages source
        JOIN wiki_pages concept ON concept.workspace_id = source.workspace_id
        LEFT JOIN wiki_source_manual_links manual
          ON manual.from_page_id = source.id AND manual.to_page_id = concept.id
        WHERE source.workspace_id = %(ws)s
          AND source.page_type = 'source' AND source.status = 'active'
          AND concept.page_type = 'concept' AND concept.status = 'active'
          AND (concept.id = ANY(%(pages)s) OR source.id = ANY(%(sources)s))
          AND (
              manual.present
              OR (manual.present IS NULL AND EXISTS (
                  SELECT 1
                  FROM document_wiki_links source_of
                  JOIN document_wiki_links extracted
                    ON extracted.document_id = source_of.document_id
                   AND extracted.relation_type = 'extracted_concept'
                   AND extracted.wiki_page_id = concept.id
                  WHERE source_of.wiki_page_id = source.id
                    AND source_of.relation_type = 'source_of'
                    AND NOT EXISTS (
                        SELECT 1 FROM wiki_source_tombstones tombstone
                        WHERE tombstone.workspace_id = %(ws)s AND tombstone.document_id = source_of.document_id
                    )
              ))
          )
        ON CONFLICT (from_page_id, link_type, to_page_id) DO NOTHING
        """,
        scope,
    )


def delete_source_related_links(
    conn: psycopg.Connection,
    workspace_id: str,
) -> None:
    conn.execute(
        """
        DELETE FROM wiki_page_links l
        USING wiki_pages from_page, wiki_pages to_page
        WHERE l.link_type = 'source_related_to'
          AND from_page.id = l.from_page_id
          AND to_page.id = l.to_page_id
          AND from_page.page_type = 'source'
          AND to_page.page_type = 'source'
          AND from_page.workspace_id = %s
          AND to_page.workspace_id = %s
        """,
        (workspace_id, workspace_id),
    )


def load_existing_concept_ids_by_slug(
    conn: psycopg.Connection,
    workspace_id: str,
) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT slug, id
        FROM wiki_pages
        WHERE page_type = 'concept'
          AND status = 'active'
          AND workspace_id = %s
        """,
        (workspace_id,),
    ).fetchall()
    return {row["slug"]: row["id"] for row in rows}


def resolve_or_create_wiki_page_id(
    conn: psycopg.Connection,
    user_id: str,
    workspace_id: str,
    page_type: str,
    slug: str,
) -> str:
    slug = slugify(slug)
    candidate = f"wiki_page_{uuid.uuid4()}"
    row = conn.execute(
        """
        INSERT INTO wiki_pages (
            id, page_type, title, slug, summary, markdown_uri,
            user_id, workspace_id, status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, NULL, NULL, %s, %s, 'draft', now(), now())
        ON CONFLICT (workspace_id, page_type, slug) DO UPDATE
            SET updated_at = wiki_pages.updated_at
        RETURNING id
        """,
        (candidate, page_type, slug, slug, user_id, workspace_id),
    ).fetchone()
    return str(row["id"])


def upsert_wiki_page(
    conn: psycopg.Connection,
    page_id: str,
    page_type: str,
    title: str,
    slug: str,
    summary: str,
    markdown_uri: str,
    user_id: str,
    workspace_id: str,
) -> None:
    slug = slugify(slug)
    conn.execute(
        """
        INSERT INTO wiki_pages (id, page_type, title, slug, summary, markdown_uri, user_id, workspace_id, status, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', now(), now())
        ON CONFLICT (workspace_id, page_type, slug) DO UPDATE SET
            page_type = EXCLUDED.page_type,
            title = EXCLUDED.title,
            slug = EXCLUDED.slug,
            summary = EXCLUDED.summary,
            markdown_uri = EXCLUDED.markdown_uri,
            user_id = EXCLUDED.user_id,
            workspace_id = EXCLUDED.workspace_id,
            status = 'active',
            updated_at = now()
        """,
        (
            page_id,
            page_type,
            title,
            slug,
            summary,
            markdown_uri,
            user_id,
            workspace_id,
        ),
    )


def upsert_document_wiki_link(
    conn: psycopg.Connection,
    document_id: str,
    wiki_page_id: str,
    relation_type: str,
    confidence: float | None,
    workspace_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO document_wiki_links (document_id, wiki_page_id, relation_type, confidence, workspace_id, created_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (document_id, wiki_page_id, relation_type) DO UPDATE SET
            confidence = EXCLUDED.confidence,
            workspace_id = EXCLUDED.workspace_id
        """,
        (document_id, wiki_page_id, relation_type, confidence, workspace_id),
    )


def upsert_wiki_page_link(
    conn: psycopg.Connection,
    from_page_id: str,
    to_page_id: str,
    link_type: str,
    label: str | None,
    confidence: float | None,
    workspace_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO wiki_page_links (from_page_id, to_page_id, link_type, label, confidence, workspace_id, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (from_page_id, to_page_id, link_type) DO UPDATE SET
            label = EXCLUDED.label,
            confidence = EXCLUDED.confidence,
            workspace_id = EXCLUDED.workspace_id,
            updated_at = now()
        """,
        (from_page_id, to_page_id, link_type, label, confidence, workspace_id),
    )
