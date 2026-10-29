from __future__ import annotations

import logging
from typing import Any

from app.core.jev_client import JevUnavailable
from app.modules.query.domain.scoring import hybrid_score
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.wiki_generation.application.judge_candidates import (
    judge_cluster_promotions,
    judge_meaning_cluster_candidates,
)
from app.modules.wiki_generation.application.ports import JsonCompletionPort
from app.modules.wiki_generation.domain.text_utils import slugify, unique_keep_order
from app.modules.wiki_generation.infrastructure.assemble import (
    MeaningClusterArtifactAssembler,
)
from app.modules.wiki_generation.infrastructure.jev_concept_resolver import (
    CANDIDATE_EMBEDDING_WEIGHT,
    JevConceptResolver,
    cached_embeddings,
    embedding_server_model,
    vector_similarity,
)
from app.modules.wiki_ingestion.infrastructure.active_cluster_markdown import (
    cluster_claims,
    cluster_promotion_status,
    cluster_representative,
    cluster_sections_by_id,
    merge_active_cluster_markdown,
)

# 클러스터에는 정의가 없어 대표명과 앞쪽 근거로 판정용 정의를 만든다. 앞쪽 근거는 근거가 더 쌓여도 바뀌지 않아
# 임베딩 캐시를 다시 쓰고, 개수·길이를 고정해 후보 100개가 판정 요청 몇 개에 들어가게 한다.
CLUSTER_DEFINITION_CLAIMS = 3
CLUSTER_DEFINITION_CHARS = 500
# 클러스터 판정 LLM에 보내는 기존 클러스터 분량. 넘치면 이번 후보와 가까운 클러스터부터 채운다.
ACTIVE_CLUSTER_CONTEXT_CHARS = 24000
logger = logging.getLogger(__name__)


def build_post_ingest_cluster_artifact(
    *,
    completion: JsonCompletionPort,
    normalized: dict[str, Any],
    existing_active_markdown: str,
    user_id: str,
    workspace_id: str,
    concept_update_decisions: list[dict[str, Any]],
    core_relation_decisions: list[dict[str, Any]],
    core_concepts: list[dict[str, Any]] | None = None,
    concept_judge: JevConceptResolver | None = None,
) -> dict[str, Any]:
    assembler = MeaningClusterArtifactAssembler()
    candidates = assembler.candidate_claims(normalized)
    concept_update_candidate_ids = {
        str(item.get("candidate_id"))
        for item in concept_update_decisions
        if item.get("decision") == "same_concept"
    }
    cluster_candidates = [
        item
        for item in candidates
        if item["candidate_id"] not in concept_update_candidate_ids
    ]
    cluster_decisions = judge_meaning_cluster_candidates(
        completion=completion,
        existing_active_markdown=_relevant_active_clusters(existing_active_markdown, cluster_candidates),
        candidates=cluster_candidates,
    )
    build_args = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "cluster_decisions": cluster_decisions,
        "concept_update_decisions": concept_update_decisions,
        "core_relation_decisions": core_relation_decisions,
    }
    artifact = assembler.build(normalized, **build_args)
    # 근거를 덧붙인 뒤의 클러스터를 보고 승격을 정한다.
    sections = cluster_sections_by_id(
        merge_active_cluster_markdown(existing_active_markdown, artifact["active_markdown"])
    )
    promotions = _core_concept_promotions(sections, core_concepts or [])
    if concept_judge is not None:
        promotions.update(_judged_core_concept_promotions(concept_judge, sections, core_concepts or [], promotions))
    touched = [
        {"cluster_id": cluster["id"], "section": sections[cluster["id"]]}
        for cluster in artifact["clusters"]
        if cluster["id"] in sections
        and cluster["id"] not in promotions
        and cluster_promotion_status(sections[cluster["id"]]) != "candidate"
    ]
    for cluster_id, reason in judge_cluster_promotions(completion=completion, clusters=touched).items():
        promotions[cluster_id] = _promotion(sections[cluster_id], reason)
    if not promotions:
        return artifact
    return assembler.build(normalized, **build_args, promotions=promotions)


def _core_concept_promotions(
    sections: dict[str, str],
    core_concepts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """이번 문서의 핵심 개념과 이름·별칭이 같은 클러스터를 그 개념으로 승격할 후보로 표시한다."""
    concept_by_name: dict[str, dict[str, Any]] = {}
    for concept in core_concepts:
        for name in [concept.get("slug"), concept.get("title"), *(concept.get("aliases") or [])]:
            if name:
                concept_by_name.setdefault(slugify(str(name)), concept)
    promotions: dict[str, dict[str, Any]] = {}
    for cluster_id, section in sections.items():
        if cluster_promotion_status(section) == "candidate":
            continue
        concept = concept_by_name.get(slugify(cluster_id)) or concept_by_name.get(
            slugify(cluster_representative(section) or cluster_id)
        )
        if concept and concept.get("slug"):
            title = concept.get("title") or concept["slug"]
            promotions[cluster_id] = _promotion(
                section,
                f"다른 문서에서 핵심 개념 '{title}'(으)로 추출됨",
                target=str(concept["slug"]),
            )
    return promotions


def _judged_core_concept_promotions(
    concept_judge: JevConceptResolver,
    sections: dict[str, str],
    core_concepts: list[dict[str, Any]],
    promotions: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """이름이 일치하지 않은 핵심 개념마다 클러스터와 쌍 판정해 같은 개념인 클러스터를 그 개념으로 승격할 후보로 표시한다."""
    matched = {item["target"] for item in promotions.values()}
    concepts = [
        concept
        for concept in core_concepts
        if concept.get("slug") and concept["slug"] not in matched and str(concept.get("definition") or "").strip()
    ]
    pool = [
        _cluster_concept(cluster_id, section)
        for cluster_id, section in sections.items()
        if cluster_id not in promotions and cluster_promotion_status(section) != "candidate"
    ]
    if not concepts or not pool:
        return {}
    try:
        resolutions = concept_judge.judge_concepts(concepts, pool)
    except JevUnavailable as exc:
        logger.warning("[핵심 개념-클러스터 판정 생략] %s", exc)
        return {}
    # 판정은 핵심 개념(incoming)별로 나오고, 병합 대상(canonical_slug)이 클러스터다. 관련(link_to)은 쓰지 않는다.
    judged: dict[str, dict[str, Any]] = {}
    titles = {concept["slug"]: concept.get("title") or concept["slug"] for concept in concepts}
    for concept_slug, resolution in sorted(resolutions.items()):
        cluster_id = resolution["canonical_slug"]
        if resolution["decision"] != "merge_into" or cluster_id in judged:
            continue
        judged[cluster_id] = _promotion(
            sections[cluster_id],
            f"다른 문서의 핵심 개념 '{titles[concept_slug]}'과 같은 개념으로 판정됨",
            target=concept_slug,
        )
    return judged


def _relevant_active_clusters(markdown: str, candidates: list[dict[str, Any]]) -> str:
    """기존 클러스터를 후보와 가까운 순서로 예산만큼 고른다. 섹션은 통째로 넣고, 예산보다 큰 섹션만 자른다.

    점수는 후보마다 임베딩 0.75 + BM25 0.25를 매겨 클러스터별 최고점을 쓴다. 임베딩 서버를 쓸 수 없으면
    BM25만 쓴다. 단어가 다른 동의어는 놓치지만 꼬리만 보내 앞쪽 클러스터를 아예 못 보는 것보다 낫다.
    """
    if len(markdown) <= ACTIVE_CLUSTER_CONTEXT_CHARS:
        return markdown
    sections = list(cluster_sections_by_id(markdown).items())
    documents = [f"{cluster_id} {_cluster_concept(cluster_id, section)['definition']}" for cluster_id, section in sections]
    queries = [f"{item['term']} {item['claim']}" for item in candidates]
    vectors = _embeddings([*queries, *documents])
    text_search = Bm25Searcher()
    best = [0.0] * len(documents)
    for query in queries:
        for index, lexical in enumerate(text_search.score(query, documents)):
            score = lexical
            if vectors:
                semantic = vector_similarity(vectors[query], vectors[documents[index]])
                score = hybrid_score(semantic, lexical, CANDIDATE_EMBEDDING_WEIGHT)
            best[index] = max(best[index], score)
    selected = ["# Active Meaning Clusters"]
    used = len(selected[0])
    for index in sorted(range(len(documents)), key=lambda index: -best[index]):
        remaining = ACTIVE_CLUSTER_CONTEXT_CHARS - used - 2
        section = sections[index][1]
        if len(section) > ACTIVE_CLUSTER_CONTEXT_CHARS:
            section = section[:remaining]
        if 0 < len(section) <= remaining:
            selected.append(section)
            used += len(section) + 2
    return "\n\n".join(selected)


def _embeddings(texts: list[str]) -> dict[str, Any]:
    model = embedding_server_model()
    if model is None:
        return {}
    try:
        return cached_embeddings(model, texts)
    except Exception as exc:  # 임베딩이 실패해도 클러스터 판정은 BM25 순위로 이어간다.
        logger.warning("[클러스터 판정 문맥] 임베딩 실패로 BM25만 씁니다: %s", type(exc).__name__)
        return {}


def _cluster_concept(cluster_id: str, section: str) -> dict[str, Any]:
    representative = cluster_representative(section) or cluster_id
    claims = [claim["claim"] for claim in cluster_claims(section)[:CLUSTER_DEFINITION_CLAIMS]]
    return {
        "slug": cluster_id,
        "title": representative,
        "aliases": [],
        "definition": " ".join([representative, *claims])[:CLUSTER_DEFINITION_CHARS],
    }


def _promotion(section: str, reason: str, *, target: str = "") -> dict[str, Any]:
    return {
        "representative": cluster_representative(section),
        "reason": reason,
        "target": target,
        "source_refs": unique_keep_order(
            ref.split(":", 1)[0] for claim in cluster_claims(section) for ref in claim.get("refs", []) if ":" in ref
        ),
    }
