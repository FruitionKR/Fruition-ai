TRAVERSABLE_RELATION_TYPES = frozenset(
    {
        "source_mentions_concept",
        "concept_related_to",
        "part_of",
        "child_of",
        "uses_or_depends_on",
        "contrasts_with",
        "supports_or_enables",
    }
)


def hybrid_score(embedding_score: float, text_score: float, embedding_weight: float = 0.8) -> float:
    text_weight = 1.0 - embedding_weight
    return embedding_weight * embedding_score + text_weight * text_score


def traversal_score(base_score: float, node_score: float, edge_score: float, depth: int) -> float:
    distance_penalty = 0.08 * depth
    return 0.55 * base_score + 0.35 * node_score + 0.10 * edge_score - distance_penalty


def edge_role(link_type: str) -> str:
    if link_type == "source_mentions_concept":
        return "seed_to_focus"
    if link_type == "concept_related_to":
        return "context_expansion"
    return "context_expansion"
