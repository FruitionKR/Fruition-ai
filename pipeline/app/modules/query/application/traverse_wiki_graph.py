from collections import defaultdict, deque

from app.modules.query.domain.entities import GraphContext, RetrievedPage, TraversalEdge, TraversalPath, WikiPage, WikiPageLink
from app.modules.query.domain.scoring import (
    TRAVERSABLE_RELATION_TYPES,
    edge_role,
    traversal_score,
)


class TraverseWikiGraphUseCase:
    def __init__(
        self,
        max_depth: int = 3,
        relative_score_floor: float = 0.95,
        frontier_limit: int = 8,
    ) -> None:
        self._max_depth = max_depth
        self._relative_score_floor = relative_score_floor
        self._frontier_limit = frontier_limit

    def execute(
        self,
        pages_by_id: dict[str, WikiPage],
        links: list[WikiPageLink],
        seed_page_ids: list[str],
        node_scores: dict[str, float],
    ) -> tuple[GraphContext, list[TraversalPath], str]:
        adjacency = self._build_adjacency(links)
        visited: dict[str, RetrievedPage] = {}
        edges_by_key: dict[tuple[str, str, str], TraversalEdge] = {}
        traversal_paths: list[TraversalPath] = []
        frontier = deque((seed_id, [seed_id], [], node_scores.get(seed_id, 0.0), 0) for seed_id in seed_page_ids)
        best_seed_score = max((node_scores.get(seed_id, 0.0) for seed_id in seed_page_ids), default=0.0)

        for seed_id in seed_page_ids:
            page = pages_by_id.get(seed_id)
            if page:
                visited[seed_id] = RetrievedPage(page=page, score=node_scores.get(seed_id, 0.0), role=self._node_role(page, 0), depth=0)

        if best_seed_score <= 0:
            related_pages = sorted(visited.values(), key=lambda item: item.score, reverse=True)
            return GraphContext(nodes=related_pages), [], "no_relevant_seed"

        stop_reason = "no_frontier"
        depth_limit_reached = False
        while frontier:
            current_id, path_nodes, path_edges, base_score, depth = frontier.popleft()

            next_candidates = []
            next_floor = best_seed_score * self._relative_score_floor
            for link in adjacency.get(current_id, []):
                target_id = link.to_page_id if link.from_page_id == current_id else link.from_page_id
                if target_id in path_nodes or target_id not in pages_by_id:
                    continue
                target_score = node_scores.get(target_id, 0.0)
                if target_score < next_floor:
                    continue
                next_score = traversal_score(
                    base_score=base_score,
                    node_score=target_score,
                    edge_score=link.confidence,
                    depth=depth + 1,
                )
                next_candidates.append((target_score, next_score, target_id, link))

            next_candidates.sort(key=lambda item: item[0], reverse=True)
            if depth >= self._max_depth:
                depth_limit_reached = depth_limit_reached or bool(next_candidates)
                continue
            if not next_candidates and depth == 0:
                stop_reason = "no_frontier"

            expanded = 0
            for target_score, next_score, target_id, link in next_candidates[: self._frontier_limit]:
                target = pages_by_id[target_id]
                role = self._node_role(target, depth + 1)
                previous = visited.get(target_id)
                traversal_edge = self._traversal_edge(link)
                edge_key = (traversal_edge.from_page_id, traversal_edge.to_page_id, traversal_edge.link_type)
                edges_by_key[edge_key] = traversal_edge
                if previous is not None and previous.score >= target_score:
                    traversal_paths.append(
                        self._traversal_path(traversal_paths, path_nodes, path_edges, target_id, traversal_edge, target_score)
                    )
                    continue
                visited[target_id] = RetrievedPage(page=target, score=target_score, role=role, depth=depth + 1)

                next_path_nodes = [*path_nodes, target_id]
                next_path_edges = [*path_edges, traversal_edge]
                traversal_paths.append(
                    self._traversal_path(traversal_paths, path_nodes, path_edges, target_id, traversal_edge, target_score)
                )
                frontier.append((target_id, next_path_nodes, next_path_edges, next_score, depth + 1))
                expanded += 1

            if expanded:
                stop_reason = "no_frontier"
            elif next_candidates:
                stop_reason = "relative_score_floor"

        if depth_limit_reached:
            stop_reason = "max_depth"
        related_pages = sorted(visited.values(), key=lambda item: item.score, reverse=True)
        graph_context = GraphContext(nodes=related_pages, edges=list(edges_by_key.values()))
        return graph_context, sorted(traversal_paths, key=lambda item: item.score, reverse=True), stop_reason

    @staticmethod
    def _traversal_edge(link: WikiPageLink) -> TraversalEdge:
        return TraversalEdge(
            from_page_id=link.from_page_id,
            to_page_id=link.to_page_id,
            link_type=link.link_type,
            role=edge_role(link.link_type),
            score=link.confidence,
        )

    @staticmethod
    def _traversal_path(
        existing_paths: list[TraversalPath],
        path_nodes: list[str],
        path_edges: list[TraversalEdge],
        target_id: str,
        traversal_edge: TraversalEdge,
        target_score: float,
    ) -> TraversalPath:
        return TraversalPath(
            path_id=f"path_{len(existing_paths) + 1}",
            role="primary_answer_path" if not existing_paths else "candidate_path",
            nodes=[*path_nodes, target_id],
            edges=[*path_edges, traversal_edge],
            score=target_score,
        )

    def _build_adjacency(self, links: list[WikiPageLink]) -> dict[str, list[WikiPageLink]]:
        adjacency: dict[str, list[WikiPageLink]] = defaultdict(list)
        for link in links:
            if link.link_type not in TRAVERSABLE_RELATION_TYPES:
                continue
            adjacency[link.from_page_id].append(link)
            adjacency[link.to_page_id].append(link)
        return adjacency

    def _node_role(self, page: WikiPage, depth: int) -> str:
        if page.is_source and depth == 0:
            return "seed_source"
        if page.is_source:
            return "supporting_source"
        if page.is_concept and depth <= 1:
            return "focus_concept"
        return "related_context"
