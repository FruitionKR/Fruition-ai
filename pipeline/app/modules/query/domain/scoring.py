import re


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


# Jev 한영 교차 실험에서 같은 wiki 단위의 근거를 가장 앞에 찾은 키워드(BM25) 비중이다.
# 한국어 질문은 영어 원문 근거와 표기가 달라 키워드 일치가 적고, 영어 질문은 원문 용어가 그대로 맞는다.
KOREAN_KEYWORD_WEIGHT = 0.10
ENGLISH_KEYWORD_WEIGHT = 0.40
_HANGUL = re.compile(r"[\uac00-\ud7a3\u3131-\u318e]")


def evidence_embedding_weight(question: str) -> float:
    keyword_weight = KOREAN_KEYWORD_WEIGHT if _HANGUL.search(question) else ENGLISH_KEYWORD_WEIGHT
    return 1.0 - keyword_weight


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
