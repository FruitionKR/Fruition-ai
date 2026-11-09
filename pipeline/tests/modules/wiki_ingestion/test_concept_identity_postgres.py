import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.modules.wiki_generation.domain.concept_identity import ConceptSlugExhausted
from app.modules.wiki_generation.infrastructure.source_context_merge import (
    source_page_context_parts,
)
from app.modules.wiki_ingestion.application.concept_contribution import (
    build_concept_contributions,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import (
    _cleanup_deleted_wiki_pages,
)
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_output_persistence import _prepare_concept_page_ids
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import (
    load_concept_identity_index,
    resolve_concept_page_identity,
    upsert_concept_identity_aliases,
    upsert_wiki_page,
)


DSN = os.getenv("TEST_DATABASE_URL", "")
AI_SCHEMA_SQL = (Path(__file__).resolve().parents[3] / "db" / "ai_schema.sql").read_text(encoding="utf-8")


@pytest.fixture
def scoped_dsn():
    if not DSN:
        pytest.skip("isolated PostgreSQL test URL is not configured")
    schema = "test_concept_identity_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in DSN else "?"
    scoped = f"{DSN}{separator}options={quote(f'-csearch_path={schema}')}"
    with psycopg.connect(scoped) as conn:
        conn.execute(AI_SCHEMA_SQL)
    try:
        yield scoped
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _activate(
    conn: psycopg.Connection,
    *,
    workspace: str,
    title: str,
    preferred: str,
    aliases: list[str] | None = None,
) -> tuple[str, str, str]:
    aliases = aliases or []
    page_id, slug, prior = resolve_concept_page_identity(
        conn,
        "user-1",
        workspace,
        title=title,
        identity_aliases=aliases,
        preferred_slug=preferred,
    )
    upsert_wiki_page(conn, page_id, "concept", title, slug, title, f"wiki/{slug}.md", "user-1", workspace)
    upsert_concept_identity_aliases(conn, workspace, page_id, aliases)
    return page_id, slug, prior


def _insert_page(
    conn: psycopg.Connection,
    *,
    page_id: str,
    workspace: str,
    title: str,
    slug: str,
    status: str,
    page_type: str = "concept",
) -> None:
    conn.execute(
        """
        INSERT INTO wiki_pages
            (id, page_type, title, slug, user_id, workspace_id, status, created_at, updated_at)
        VALUES (%s, %s, %s, %s, 'user-1', %s, %s, now(), now())
        """,
        (page_id, page_type, title, slug, workspace, status),
    )


def test_concurrent_same_name_allocation_serializes_to_one_page(scoped_dsn: str) -> None:
    barrier = Barrier(2)

    def allocate() -> tuple[str, str, str]:
        with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
            barrier.wait(timeout=5)
            return _activate(conn, workspace="ws-lock", title="C#", preferred="c")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: allocate(), range(2)))

    assert {(page_id, slug) for page_id, slug, _prior in results} == {(results[0][0], "c")}
    assert {prior for _page_id, _slug, prior in results} == {"new", "active"}
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        assert conn.execute("SELECT count(*) AS count FROM wiki_pages WHERE workspace_id = 'ws-lock'").fetchone()["count"] == 1


def test_deleted_identity_revival_and_manual_link_isolation(scoped_dsn: str) -> None:
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        original_id, original_slug, _ = _activate(conn, workspace="ws-delete", title="C", preferred="c")
        _insert_page(
            conn,
            page_id="source-1",
            workspace="ws-delete",
            title="Source",
            slug="source",
            status="active",
            page_type="source",
        )
        conn.execute(
            "INSERT INTO wiki_source_manual_links (from_page_id, to_page_id, present, workspace_id) VALUES ('source-1', %s, true, 'ws-delete')",
            (original_id,),
        )
        _cleanup_deleted_wiki_pages(conn, "ws-delete", [original_id])

    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        revived_id, revived_slug, prior = _activate(conn, workspace="ws-delete", title="C", preferred="c")
        assert (revived_id, revived_slug, prior) == (original_id, original_slug, "deleted")
        _cleanup_deleted_wiki_pages(conn, "ws-delete", [revived_id])
        repeated_id, repeated_slug, prior = _activate(conn, workspace="ws-delete", title="C", preferred="c")
        assert (repeated_id, repeated_slug, prior) == (original_id, original_slug, "deleted")
        _cleanup_deleted_wiki_pages(conn, "ws-delete", [repeated_id])

    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        different_id, different_slug, prior = _activate(conn, workspace="ws-delete", title="C#", preferred="c")
        assert prior == "new"
        assert different_id != original_id and different_slug == "c-2"
        _insert_page(
            conn,
            page_id="second-deleted-c",
            workspace="ws-delete",
            title="C",
            slug="c-3",
            status="deleted",
        )

    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        ambiguous_id, ambiguous_slug, prior = _activate(conn, workspace="ws-delete", title="C", preferred="c")
        assert prior == "new"
        assert ambiguous_id not in {original_id, "second-deleted-c"}
        assert ambiguous_slug == "c-4"
        manual_target = conn.execute("SELECT to_page_id FROM wiki_source_manual_links").fetchone()["to_page_id"]
        assert manual_target == original_id
        assert manual_target not in {different_id, ambiguous_id}


def test_alias_provenance_and_exhausted_candidates(scoped_dsn: str) -> None:
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        page_id, _slug, _ = _activate(
            conn,
            workspace="ws-alias",
            title="Canonical",
            preferred="canonical",
            aliases=["Known Alias"],
        )
        [indexed] = load_concept_identity_index(conn, "ws-alias", include_deleted=True)
        assert indexed["id"] == page_id
        assert indexed["identity_aliases"] == ["Known Alias"]

        for index, slug in enumerate(["c", *(f"c-{number}" for number in range(2, 10))]):
            _insert_page(
                conn,
                page_id=f"occupied-{index}",
                workspace="ws-exhausted",
                title=f"Other {index}",
                slug=slug,
                status="active",
            )
        with pytest.raises(ConceptSlugExhausted, match="c"):
            resolve_concept_page_identity(
                conn,
                "user-1",
                "ws-exhausted",
                title="C++",
                identity_aliases=[],
                preferred_slug="c",
            )


def test_persistence_allocation_remaps_pages_contributions_evidence_and_links(scoped_dsn: str) -> None:
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        _activate(conn, workspace="ws-map", title="C", preferred="c")
        normalized, incoming = source_page_context_parts({
            "document": {"document_id": "doc-new"},
            "concept_ledger": [{
                "slug": "c", "title": "C#", "identity_aliases": [],
                "anchor_reference_ids": ["B1"], "evidence_claim_ids": ["ev-new"],
            }],
            "evidence_units": [{
                "evidence_id": "ev-new", "claim": "Sharp evidence",
                "anchor_reference_ids": ["B1"], "related_concept_slugs": ["c"],
                "source_document_id": "doc-new",
            }],
            "entry_changes": {"c": [{"op": "add", "entry_id": "entry:sharp", "slug": "c"}]},
            "concept_slug_map": {"c": "c"},
            "hint_slug_map": {"C#": "c"},
            "entry_references": [{"source": "concept:c", "target": "concept:other"}],
            "alias_concept_links": [],
            "observations": [], "section_candidates": [], "mentions": [],
        }, {
            "document_id": "doc-old",
            "core_concepts": [{"term": "C", "slug": "c", "identity_aliases": []}],
            "evidence_claims": [], "section_candidates": [], "mentions": [],
            "source_markdown": "[[c|C]]",
        })
        links = [
            {"source": "source:doc-new", "target": "concept:c-2", "relation": "mentions"},
            {"source": "concept:c-2", "target": "concept:other", "relation": "related_to"},
        ]
        contributions = build_concept_contributions(
            operation_id="op-new",
            normalized=incoming,
            source_blocks=[{"block_id": "B1", "text": "Sharp evidence"}],
            links=links,
        )
        manifest = {
            "concept_pages": [{"slug": "c-2", "title": "C#", "markdown": "---\nslug: c-2\n---\n[[c-2]]\n"}],
            "source_page": {"markdown": "[[c-2|C#]]"},
            "concept_contributions": contributions,
            "links": links,
        }
        page_ids = _prepare_concept_page_ids(conn, manifest, normalized, "user-1", "ws-map")

        assert set(page_ids) == {"c", "c-2"}
        assert [(concept["title"], concept["slug"]) for concept in normalized["concept_ledger"]] == [("C", "c"), ("C#", "c-2")]
        assert incoming["evidence_units"][0]["related_concept_slugs"] == ["c-2"]
        assert manifest["concept_pages"][0]["slug"] == "c-2"
        assert "slug: c-2" in manifest["concept_pages"][0]["markdown"]
        assert manifest["source_page"]["markdown"] == "[[c-2|C#]]"
        assert list(manifest["concept_contributions"]) == ["c-2"]
        assert manifest["concept_contributions"]["c-2"]["concept"]["slug"] == "c-2"
        assert manifest["concept_contributions"]["c-2"]["evidence_units"][0]["related_concept_slugs"] == ["c-2"]
        assert manifest["concept_contributions"]["c-2"]["entry_changes"][0]["slug"] == "c-2"
        assert manifest["concept_contributions"]["c-2"]["links"] == [
            {"source": "concept:c-2", "target": "concept:other", "relation": "related_to"},
        ]
        assert manifest["links"][0]["target"] == "concept:c-2"
        assert normalized["existing_source_context"]["source_markdown"] == "[[c|C]]"


@pytest.mark.parametrize("scope_type,scope_id", [("workspace", "ws-purged"), ("user", "user-1")])
def test_purged_scope_fence_blocks_identity_allocation(scoped_dsn: str, scope_type: str, scope_id: str) -> None:
    with psycopg.connect(scoped_dsn, row_factory=dict_row) as conn:
        conn.execute(
            "INSERT INTO ai_purged_scopes (scope_type, scope_id, purged_at) VALUES (%s, %s, now())",
            (scope_type, scope_id),
        )
        with pytest.raises(RuntimeError, match="purged"):
            resolve_concept_page_identity(
                conn,
                "user-1",
                "ws-purged",
                title="C",
                identity_aliases=[],
                preferred_slug="c",
            )
