from dataclasses import replace

from app.modules.query.domain.entities import (
    GraphContext,
    RetrievedPage,
    TraversalEdge,
    TraversalPath,
    WikiPage,
    WikiPageLink,
)


def add_focus_concepts_to_related_pages(
    related_pages: list[RetrievedPage],
    focus_concept_ids: list[str],
    pages_by_id: dict[str, WikiPage],
    concept_scores: dict[str, float],
) -> list[RetrievedPage]:
    merged = list(related_pages)
    seen = {item.page.id for item in merged}
    for concept_id in focus_concept_ids:
        if concept_id in seen or concept_id not in pages_by_id:
            continue
        merged.append(
            RetrievedPage(
                page=pages_by_id[concept_id],
                score=concept_scores.get(concept_id, 0.0),
                role="focus_concept",
                depth=0,
            )
        )
        seen.add(concept_id)
    return sorted(merged, key=lambda item: item.score, reverse=True)


def backfill_direct_concept_paths(
    graph_context: GraphContext,
    traversal_paths: list[TraversalPath],
    links: list[WikiPageLink],
    direct_concept_ids: list[str],
    source_scores: dict[str, float],
    concept_scores: dict[str, float],
) -> tuple[GraphContext, list[TraversalPath]]:
    if not direct_concept_ids:
        return graph_context, traversal_paths

    related_ids = {item.page.id for item in graph_context.nodes}
    existing_edge_keys = {(edge.from_page_id, edge.to_page_id, edge.link_type) for edge in graph_context.edges}
    existing_path_pairs = {
        (path.nodes[0], path.nodes[-1])
        for path in traversal_paths
        if len(path.nodes) >= 2
    }
    edges = list(graph_context.edges)
    paths = list(traversal_paths)

    for link in links:
        if link.link_type != "source_mentions_concept":
            continue
        if link.to_page_id not in direct_concept_ids:
            continue
        if link.from_page_id not in related_ids or link.to_page_id not in related_ids:
            continue

        edge_key = (link.from_page_id, link.to_page_id, link.link_type)
        score = float(link.confidence or 1.0)
        traversal_edge = TraversalEdge(
            from_page_id=link.from_page_id,
            to_page_id=link.to_page_id,
            link_type=link.link_type,
            role="seed_to_focus",
            score=score,
        )
        if edge_key not in existing_edge_keys:
            edges.append(traversal_edge)
            existing_edge_keys.add(edge_key)

        path_pair = (link.from_page_id, link.to_page_id)
        if path_pair in existing_path_pairs:
            continue
        path_score = max(source_scores.get(link.from_page_id, 0.0), concept_scores.get(link.to_page_id, 0.0))
        paths.append(
            TraversalPath(
                path_id=f"direct_concept_path_{len(paths) + 1}",
                role="primary_answer_path" if not paths else "candidate_path",
                nodes=[link.from_page_id, link.to_page_id],
                edges=[traversal_edge],
                score=path_score,
                used_for_answer=True,
                stop_reason="concept_direct_match",
            )
        )
        existing_path_pairs.add(path_pair)

    return GraphContext(nodes=graph_context.nodes, edges=edges), paths


def add_sources_connected_to_focus_concepts(
    seed_source_ids: list[str],
    focus_concept_ids: list[str],
    links: list[WikiPageLink],
) -> list[str]:
    seeds = list(dict.fromkeys(seed_source_ids))
    focus_set = set(focus_concept_ids)
    for link in links:
        if link.link_type != "source_mentions_concept":
            continue
        if link.to_page_id in focus_set and link.from_page_id not in seeds:
            seeds.append(link.from_page_id)
        elif link.from_page_id in focus_set and link.to_page_id not in seeds:
            seeds.append(link.to_page_id)
    return seeds


def select_answer_paths(traversal_paths: list[TraversalPath], returned_path_limit: int) -> list[TraversalPath]:
    selected = sorted(traversal_paths, key=lambda path: path.score, reverse=True)[:returned_path_limit]
    return [
        replace(path, role="primary_answer_path" if index == 0 else "candidate_path")
        for index, path in enumerate(selected)
    ]
