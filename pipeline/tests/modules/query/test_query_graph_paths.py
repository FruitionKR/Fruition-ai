from app.modules.query.application.query_graph_paths import (
    add_focus_concepts_to_related_pages,
    add_sources_connected_to_focus_concepts,
    backfill_direct_concept_paths,
    select_answer_paths,
)
from app.modules.query.domain.entities import (
    GraphContext,
    RetrievedPage,
    TraversalEdge,
    TraversalPath,
    WikiPage,
    WikiPageLink,
)


def _page(page_id: str, page_type: str = "concept") -> WikiPage:
    return WikiPage(
        id=page_id,
        page_type=page_type,
        title=page_id,
        slug=page_id.replace(":", "-"),
        summary="",
    )


def test_add_focus_concepts_to_related_pages_merges_missing_concepts_by_score() -> None:
    source = RetrievedPage(page=_page("source:one", "source"), score=0.4, role="seed", depth=0)
    pages_by_id = {
        "source:one": source.page,
        "concept:one": _page("concept:one"),
        "concept:two": _page("concept:two"),
    }

    related_pages = add_focus_concepts_to_related_pages(
        related_pages=[source],
        focus_concept_ids=["concept:one", "concept:two"],
        pages_by_id=pages_by_id,
        concept_scores={"concept:one": 0.9, "concept:two": 0.7},
    )

    assert [item.page.id for item in related_pages] == ["concept:one", "concept:two", "source:one"]
    assert related_pages[0].role == "focus_concept"
    assert related_pages[0].depth == 0


def test_backfill_direct_concept_paths_adds_missing_source_to_concept_path() -> None:
    graph_context = GraphContext(
        nodes=[
            RetrievedPage(page=_page("source:seed", "source"), score=0.5, role="seed", depth=0),
            RetrievedPage(page=_page("concept:focus"), score=0.8, role="focus_concept", depth=0),
        ],
        edges=[],
    )

    updated_context, traversal_paths = backfill_direct_concept_paths(
        graph_context=graph_context,
        traversal_paths=[],
        links=[WikiPageLink("source:seed", "concept:focus", "source_mentions_concept", confidence=0.75)],
        direct_concept_ids=["concept:focus"],
        source_scores={"source:seed": 0.5},
        concept_scores={"concept:focus": 0.8},
    )

    assert len(updated_context.edges) == 1
    assert updated_context.edges[0].role == "seed_to_focus"
    assert traversal_paths[0].nodes == ["source:seed", "concept:focus"]
    assert traversal_paths[0].role == "primary_answer_path"
    assert traversal_paths[0].score == 0.8
    assert traversal_paths[0].stop_reason == "concept_direct_match"


def test_add_sources_connected_to_focus_concepts_keeps_existing_order_and_unique_values() -> None:
    seed_source_ids = add_sources_connected_to_focus_concepts(
        seed_source_ids=["source:existing", "source:existing"],
        focus_concept_ids=["concept:focus"],
        links=[
            WikiPageLink("source:existing", "concept:focus", "source_mentions_concept"),
            WikiPageLink("source:new", "concept:focus", "source_mentions_concept"),
            WikiPageLink("source:ignored", "concept:other", "source_mentions_concept"),
        ],
    )

    assert seed_source_ids == ["source:existing", "source:new"]


def test_select_answer_paths_limits_and_relabels_by_score() -> None:
    paths = [
        TraversalPath("path-low", "old", ["a"], [], score=0.2),
        TraversalPath(
            "path-high",
            "old",
            ["b", "c"],
            [TraversalEdge("b", "c", "related", "old", score=0.9)],
            score=0.9,
        ),
        TraversalPath("path-mid", "old", ["d"], [], score=0.5),
    ]

    selected = select_answer_paths(paths, returned_path_limit=2)

    assert [path.path_id for path in selected] == ["path-high", "path-mid"]
    assert [path.role for path in selected] == ["primary_answer_path", "candidate_path"]
