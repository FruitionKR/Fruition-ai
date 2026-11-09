from __future__ import annotations

from app.modules.mcp_read.application.wiki_search import WikiSearchCandidate, WikiSearchError
from app.modules.query.domain.entities import SemanticQueryEmbedding
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database


SEARCH_SQL = """
WITH search_query AS MATERIALIZED (
    SELECT %s::text AS raw, plainto_tsquery('simple', %s) AS value
),
scope_pages_probe AS MATERIALIZED (
    SELECT
        page.id,
        octet_length(coalesce(page.title, ''))
            + octet_length(page.slug)
            + octet_length(coalesce(page.summary, '')) AS metadata_bytes,
        page_vector.embedding_dimension > %s
            OR cardinality(page_vector.embedding_vector) > %s AS vector_oversized
    FROM wiki_pages page
    JOIN wiki_page_embeddings page_vector
      ON page_vector.page_id = page.id
     AND page_vector.embedding_model = %s
     AND page_vector.status = 'completed'
     AND page_vector.updated_at >= page.updated_at
     AND page_vector.representation_hash ~ '^[0-9a-f]{64}$'
    WHERE page.workspace_id = %s
      AND page.status = 'active'
      AND page.page_type IN ('source', 'concept')
    LIMIT %s
),
page_admission AS MATERIALIZED (
    SELECT
        count(*) <= %s
            AND coalesce(sum(metadata_bytes), 0) <= %s
            AND NOT coalesce(bool_or(vector_oversized), false) AS allowed,
        coalesce(sum(metadata_bytes), 0) AS page_bytes
    FROM scope_pages_probe
),
scope_units_probe AS MATERIALIZED (
    SELECT
        unit.id,
        unit.page_id,
        octet_length(unit.text) AS text_bytes,
        unit_vector.embedding_dimension > %s
            OR cardinality(unit_vector.embedding_vector) > %s AS vector_oversized
    FROM page_admission admission
    JOIN scope_pages_probe page ON admission.allowed
    JOIN wiki_embedding_units unit ON unit.page_id = page.id
    LEFT JOIN wiki_embedding_vectors unit_vector
      ON unit_vector.id = unit.embedding_vector_id
     AND unit_vector.embedding_model = %s
     AND unit_vector.status = 'completed'
    LIMIT %s
),
unit_admission AS MATERIALIZED (
    SELECT
        count(*) AS unit_count,
        coalesce(sum(text_bytes), 0) AS unit_bytes,
        coalesce(bool_or(vector_oversized), false) AS vector_oversized
    FROM scope_units_probe
),
admission AS MATERIALIZED (
    SELECT
        page.allowed
            AND unit.unit_count <= %s
            AND page.page_bytes + unit.unit_bytes <= %s
            AND NOT unit.vector_oversized AS allowed
    FROM page_admission page
    CROSS JOIN unit_admission unit
),
admitted_pages AS MATERIALIZED (
    SELECT
        page.id,
        page.page_type,
        page.title,
        page.slug,
        coalesce(page.summary, '') AS summary,
        page.updated_at,
        'sha256:' || page_vector.representation_hash AS content_hash,
        page_vector.embedding_vector,
        page_vector.embedding_dimension
    FROM admission
    JOIN scope_pages_probe scope ON admission.allowed
    JOIN wiki_pages page ON page.id = scope.id
    JOIN wiki_page_embeddings page_vector
      ON page_vector.page_id = page.id
     AND page_vector.embedding_model = %s
),
admitted_units AS MATERIALIZED (
    SELECT
        unit.id,
        unit.page_id,
        unit.source_document_id,
        unit.unit_type,
        unit.block_refs,
        unit.text,
        unit.weight,
        unit_vector.embedding_vector,
        unit_vector.embedding_dimension
    FROM admission
    JOIN scope_units_probe scope ON admission.allowed
    JOIN wiki_embedding_units unit ON unit.id = scope.id
    LEFT JOIN wiki_embedding_vectors unit_vector
      ON unit_vector.id = unit.embedding_vector_id
     AND unit_vector.embedding_model = %s
     AND unit_vector.status = 'completed'
),
lexical_scores AS MATERIALIZED (
    SELECT
        page.id,
        CASE
            WHEN lower(page.title) = lower(search_query.raw)
              OR lower(page.slug) = lower(search_query.raw)
            THEN 1 ELSE 0
        END AS exact_match,
        greatest(
            ts_rank_cd(
                to_tsvector(
                    'simple',
                    coalesce(page.title, '') || ' ' || page.slug || ' ' || page.summary
                ),
                search_query.value
            ),
            coalesce(max(ts_rank_cd(to_tsvector('simple', unit.text), search_query.value)), 0)
        ) AS lexical_score
    FROM admitted_pages page
    CROSS JOIN search_query
    LEFT JOIN admitted_units unit ON unit.page_id = page.id
    GROUP BY page.id, page.title, page.slug, page.summary, search_query.raw, search_query.value
),
semantic_page_scores AS MATERIALIZED (
    SELECT
        page.id,
        sum(stored.value * query_vector.value)
        / nullif(
            sqrt(sum(stored.value * stored.value))
            * sqrt(sum(query_vector.value * query_vector.value)),
            0
        ) AS similarity
    FROM admitted_pages page
    CROSS JOIN LATERAL unnest(page.embedding_vector) WITH ORDINALITY AS stored(value, ordinal)
    JOIN unnest(%s::double precision[]) WITH ORDINALITY AS query_vector(value, ordinal) USING (ordinal)
    WHERE page.embedding_dimension = %s
    GROUP BY page.id
),
candidate_pages AS MATERIALIZED (
    SELECT
        page.*,
        lexical.exact_match,
        lexical.lexical_score,
        coalesce(semantic.similarity, 0) AS page_similarity
    FROM admitted_pages page
    JOIN lexical_scores lexical ON lexical.id = page.id
    LEFT JOIN semantic_page_scores semantic ON semantic.id = page.id
    ORDER BY
        lexical.exact_match DESC,
        greatest(lexical.lexical_score, coalesce(semantic.similarity, 0)) DESC,
        page.updated_at DESC,
        page.id
    LIMIT %s
),
candidate_units AS MATERIALIZED (
    SELECT
        page.*,
        coalesce(best.text, '') AS embedding_text,
        best.id AS unit_id,
        best.unit_type,
        best.weight AS unit_weight,
        best.source_document_id,
        coalesce((best.block_refs)[1:%s], ARRAY[]::text[]) AS block_refs,
        coalesce(cardinality(best.block_refs), 0) AS evidence_count,
        best.embedding_vector AS unit_embedding_vector,
        best.embedding_dimension AS unit_embedding_dimension,
        octet_length(page.title)
            + octet_length(page.summary)
            + octet_length(coalesce(best.text, '')) AS materialized_bytes
    FROM candidate_pages page
    LEFT JOIN LATERAL (
        SELECT unit.*
        FROM admitted_units unit
        CROSS JOIN search_query
        WHERE unit.page_id = page.id
        ORDER BY
            ts_rank_cd(to_tsvector('simple', unit.text), search_query.value) DESC,
            unit.weight DESC,
            unit.id
        LIMIT 1
    ) best ON true
),
final_rows AS MATERIALIZED (
    SELECT
        page.id,
        page.page_type,
        page.title,
        page.summary,
        greatest(1, floor(extract(epoch FROM page.updated_at) * 1000000)::bigint) AS version,
        page.content_hash,
        page.embedding_text,
        page.unit_id,
        page.unit_type,
        page.unit_weight,
        page.source_document_id,
        page.block_refs,
        page.evidence_count,
        page.materialized_bytes,
        coalesce(dense.similarity, 0) AS similarity,
        page.exact_match,
        page.lexical_score,
        page.updated_at
    FROM candidate_units page
    LEFT JOIN LATERAL (
        SELECT
            sum(stored.value * query_vector.value)
            / nullif(
                sqrt(sum(stored.value * stored.value))
                * sqrt(sum(query_vector.value * query_vector.value)),
                0
            ) AS similarity
        FROM unnest(page.unit_embedding_vector) WITH ORDINALITY AS stored(value, ordinal)
        JOIN unnest(%s::double precision[]) WITH ORDINALITY AS query_vector(value, ordinal) USING (ordinal)
        WHERE page.unit_embedding_dimension = %s
    ) dense ON true
)
SELECT
    false AS capacity_exceeded,
    id,
    page_type,
    title,
    summary,
    version,
    content_hash,
    embedding_text,
    unit_id,
    unit_type,
    unit_weight,
    source_document_id,
    block_refs,
    evidence_count,
    materialized_bytes,
    similarity
FROM final_rows
UNION ALL
SELECT
    true,
    NULL::varchar(255),
    NULL::varchar(255),
    NULL::text,
    NULL::text,
    NULL::bigint,
    NULL::text,
    NULL::text,
    NULL::text,
    NULL::text,
    NULL::double precision,
    NULL::varchar(255),
    NULL::text[],
    NULL::integer,
    NULL::bigint,
    NULL::double precision
FROM admission
WHERE NOT admission.allowed
"""


class PostgresMcpWikiSearchRepository:
    def list_candidates(
        self,
        *,
        workspace_id: str,
        query: str,
        semantic_query: SemanticQueryEmbedding,
        candidate_limit: int,
        materialized_bytes_limit: int,
        evidence_limit: int,
        statement_timeout_ms: int,
        scan_page_limit: int,
        scan_unit_limit: int,
        vector_dimension_limit: int,
    ) -> list[WikiSearchCandidate]:
        with database.connect() as conn:
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{statement_timeout_ms}ms",),
            )
            rows = conn.execute(
                SEARCH_SQL,
                (
                    query,
                    query,
                    vector_dimension_limit,
                    vector_dimension_limit,
                    semantic_query.model_name,
                    workspace_id,
                    scan_page_limit + 1,
                    scan_page_limit,
                    materialized_bytes_limit,
                    vector_dimension_limit,
                    vector_dimension_limit,
                    semantic_query.model_name,
                    scan_unit_limit + 1,
                    scan_unit_limit,
                    materialized_bytes_limit,
                    semantic_query.model_name,
                    semantic_query.model_name,
                    semantic_query.vector,
                    len(semantic_query.vector),
                    candidate_limit,
                    evidence_limit + 1,
                    semantic_query.vector,
                    len(semantic_query.vector),
                ),
            ).fetchall()
        if rows and rows[0]["capacity_exceeded"]:
            raise WikiSearchError(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "Wiki search scope exceeds configured capacity.",
                retryable=False,
            )
        return [
            WikiSearchCandidate(
                page_id=str(row["id"]),
                page_type=str(row["page_type"]),
                title=str(row["title"]),
                summary=str(row["summary"] or ""),
                version=int(row["version"]),
                content_hash=str(row["content_hash"]),
                embedding_text=str(row["embedding_text"] or ""),
                unit_id=str(row["unit_id"]) if row.get("unit_id") else None,
                unit_type=str(row.get("unit_type") or ""),
                unit_weight=float(row.get("unit_weight") or 1.0),
                source_document_id=(str(row["source_document_id"]) if row.get("source_document_id") else None),
                block_refs=tuple(str(value) for value in (row["block_refs"] or [])),
                evidence_count=int(row["evidence_count"]),
                materialized_bytes=int(row["materialized_bytes"]),
                semantic_score=float(row["similarity"] or 0.0),
            )
            for row in rows
        ]
