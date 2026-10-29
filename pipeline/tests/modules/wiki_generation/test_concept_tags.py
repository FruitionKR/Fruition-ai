from app.modules.wiki_generation.infrastructure.assemble import ConceptPageAssembler
from app.modules.wiki_generation.infrastructure.concept_resolution import (
    load_existing_concept_index,
    merge_concept_ledger,
)
from app.modules.wiki_generation.infrastructure.extract import MarkdownBlockExtractor
from app.modules.wiki_generation.infrastructure.normalize import SemanticNormalizer
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import _merge_contributions
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository import (
    _concept_index_from_markdown,
)


def _normalize(core_concepts: list[dict]) -> dict:
    document, blocks = MarkdownBlockExtractor().blocks_from_records(
        [{"block_id": "B0001", "text": "Alpha는 첫 번째 개념이다."}],
        text="Alpha는 첫 번째 개념이다.\n",
        source_path="doc.md",
        fallback_title="doc",
    )
    note = {"chunk_id": "chunk_0001", "core_concepts": core_concepts}
    return SemanticNormalizer(document, blocks).normalize_notes([note])


def _concept(**extra) -> dict:
    return {"title": "Alpha", "slug_hint": "alpha", "evidence_block_ids": ["B0001"], **extra}


def test_extraction_tags_are_cleaned_and_limited() -> None:
    normalized = _normalize(
        [_concept(aliases=["A"], tags=[" 분야 ", "분야", "alpha", "a", "", "분류", "영역", "초과"])]
    )

    assert normalized["concept_ledger"][0]["tags"] == ["분야", "분류", "영역"]


def test_same_slug_concepts_union_tags_up_to_three() -> None:
    normalized = _normalize([_concept(tags=["분야"]), _concept(tags=["분야", "분류", "영역", "초과"])])

    assert normalized["concept_ledger"][0]["tags"] == ["분야", "분류", "영역"]


def test_resolution_merge_unions_tags() -> None:
    ledger = [
        {"slug": "alpha", "title": "Alpha", "aliases": [], "tags": ["분야"]},
        {"slug": "alpha-2", "title": "Alpha 2", "aliases": [], "tags": ["분야", "분류"]},
    ]

    merged = merge_concept_ledger(ledger, {}, {"alpha-2": "alpha"}, {})

    assert merged[0]["tags"] == ["분야", "분류"]


def test_frontmatter_tags_round_trip_through_concept_index(tmp_path) -> None:
    normalized = _normalize([_concept(tags=["분야", "분류"]), {**_concept(), "title": "Beta", "slug_hint": "beta"}])
    pages = {page["slug"]: page for page in ConceptPageAssembler().build_top(normalized, top_n=None)}

    for slug, page in pages.items():
        (tmp_path / "concepts").mkdir(exist_ok=True)
        (tmp_path / "concepts" / f"{slug}.md").write_text(page["markdown"], encoding="utf-8")
    file_index = {concept["slug"]: concept["tags"] for concept in load_existing_concept_index(tmp_path)}
    db_index = {
        slug: _concept_index_from_markdown(slug, page["title"], "uri", page["markdown"])["tags"]
        for slug, page in pages.items()
    }

    assert file_index == db_index == {"alpha": ["분야", "분류"], "beta": []}


def test_page_without_tags_frontmatter_reads_empty_list() -> None:
    markdown = "---\ntype: concept\nslug: alpha\n---\n\n# Alpha\n"

    assert _concept_index_from_markdown("alpha", "Alpha", "uri", markdown)["tags"] == []


def test_contribution_rebuild_unions_tags() -> None:
    merged = _merge_contributions(
        [
            {"concept": {"slug": "alpha", "title": "Alpha", "aliases": ["Alpha"], "tags": ["분야"]}},
            {"concept": {"slug": "alpha", "title": "Alpha", "aliases": ["Alpha"], "tags": ["분류", "분야"]}},
        ]
    )

    assert merged["concept_ledger"][0]["tags"] == ["분야", "분류"]
