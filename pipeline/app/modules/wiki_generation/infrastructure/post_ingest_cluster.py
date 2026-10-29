from __future__ import annotations

import logging
from typing import Any

from app.core.jev_client import JevUnavailable
from app.modules.wiki_generation.application.judge_candidates import (
    judge_cluster_promotions,
    judge_meaning_cluster_candidates,
)
from app.modules.wiki_generation.application.ports import JsonCompletionPort
from app.modules.wiki_generation.domain.text_utils import slugify, unique_keep_order
from app.modules.wiki_generation.infrastructure.assemble import (
    MeaningClusterArtifactAssembler,
)
from app.modules.wiki_generation.infrastructure.jev_concept_resolver import JevConceptResolver
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
        existing_active_markdown=existing_active_markdown,
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
