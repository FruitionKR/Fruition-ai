from __future__ import annotations

from typing import Any

from app.modules.wiki_generation.application.judge_candidates import (
    judge_cluster_promotions,
    judge_meaning_cluster_candidates,
)
from app.modules.wiki_generation.application.ports import JsonCompletionPort
from app.modules.wiki_generation.domain.text_utils import slugify, unique_keep_order
from app.modules.wiki_generation.infrastructure.assemble import (
    MeaningClusterArtifactAssembler,
)
from app.modules.wiki_ingestion.infrastructure.active_cluster_markdown import (
    cluster_claims,
    cluster_promotion_status,
    cluster_representative,
    cluster_sections_by_id,
    merge_active_cluster_markdown,
)


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
    # ponytail: 이름·별칭이 정확히 같을 때만 같은 개념으로 본다. 표기가 다른 같은 개념은 놓친다.
    # 놓치는 경우가 잦으면 애매한 점수대만 판정기(Jev)에 묻는 방식을 검토한다.
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


def _promotion(section: str, reason: str, *, target: str = "") -> dict[str, Any]:
    return {
        "representative": cluster_representative(section),
        "reason": reason,
        "target": target,
        "source_refs": unique_keep_order(
            ref.split(":", 1)[0] for claim in cluster_claims(section) for ref in claim.get("refs", []) if ":" in ref
        ),
    }
