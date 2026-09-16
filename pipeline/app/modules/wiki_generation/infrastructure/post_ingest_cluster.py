from __future__ import annotations

from typing import Any

from app.modules.wiki_generation.application.judge_candidates import (
    judge_meaning_cluster_candidates,
)
from app.modules.wiki_generation.application.ports import JsonCompletionPort
from app.modules.wiki_generation.infrastructure.assemble import (
    MeaningClusterArtifactAssembler,
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
    return assembler.build(
        normalized,
        user_id=user_id,
        workspace_id=workspace_id,
        cluster_decisions=cluster_decisions,
        concept_update_decisions=concept_update_decisions,
        core_relation_decisions=core_relation_decisions,
    )
