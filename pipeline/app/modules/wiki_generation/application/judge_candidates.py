from __future__ import annotations

import json
from typing import Any

from app.modules.wiki_generation.application.ports import JsonCompletionPort


def judge_meaning_cluster_candidates(
    *,
    completion: JsonCompletionPort,
    existing_active_markdown: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    system_prompt = """Stage=MeaningClusterJudge.
You decide whether section/mention evidence claims should update an existing term_cluster or create a new term_cluster.

Rules:
- Return JSON only.
- Do not create core concepts or graph edges.
- Prefer same_cluster when the candidate is a synonym, abbreviation, translation, spelling variant, or narrower wording of an existing cluster.
- Use new_cluster only when no existing cluster or other incoming candidate has the same meaning.
- Incoming candidates may share the same new target_cluster_id if they should be grouped together.
- target_cluster_id must be descriptive kebab-case such as "back-emf" or "manufacturing-uncertainty".
- Never use generic ids such as "cluster-001", "new-cluster", "candidate-1", or "term".
- Use needs_review only when the candidate is ambiguous.
Schema:
{
  "decisions": [
    {
      "candidate_id": "cand_001",
      "decision": "same_cluster | new_cluster | needs_review",
      "target_cluster_id": "cluster-id-to-update-or-create",
      "representative": "short display label",
      "reason": "brief Korean reason"
    }
  ]
}
"""
    payload = {
        "existing_active_clusters": existing_active_markdown,
        "incoming_candidates": [
            {
                "candidate_id": item["candidate_id"],
                "term": item["term"],
                "suggested_slug": item["slug"],
                "claim": item["claim"],
                "refs": item["refs"],
                "candidate_type": item["candidate_type"],
            }
            for item in candidates
        ],
    }
    raw = completion.complete_json(system_prompt, json.dumps(payload, ensure_ascii=False, indent=2))
    valid_candidate_ids = {item["candidate_id"] for item in candidates}
    normalized_decisions: list[dict[str, Any]] = []
    for item, candidate_id in _valid_candidate_decisions(raw, valid_candidate_ids):
        decision = str(item.get("decision") or "new_cluster")
        if decision not in {"same_cluster", "new_cluster", "needs_review"}:
            decision = "new_cluster"
        normalized_decisions.append(
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "target_cluster_id": item.get("target_cluster_id"),
                "representative": item.get("representative"),
                "reason": item.get("reason"),
            }
        )
    return normalized_decisions

PROMOTION_BATCH_CHARS = 12000
PROMOTION_PROMPT = """Stage=ClusterPromotionJudge.
Each item is one term_cluster with every evidence claim accumulated so far.

Rules:
- Return JSON only.
- Judge each cluster independently, using only its own claims. Never compare, merge, or rename clusters.
- Use promotion_status "candidate" only when the accumulated claims show a reusable domain concept worth a
  long-lived wiki page: grounded claims that define or use it consistently, with relation or retrieval
  value beyond a single document.
- Do not promote merely because a term is definable.
- Do not promote bibliographic/entity metadata: author names, researcher names, universities, funders,
  journals, conferences, publishers, citations, document titles, affiliations, or project metadata.
- Do not promote one-off named tools, software, product names, experimental labels, parameter labels, or
  isolated metrics unless the claims show a reusable domain concept.
- Otherwise use "none".
Schema:
{
  "decisions": [
    {"cluster_id": "back-emf", "promotion_status": "candidate | none", "reason": "brief Korean reason"}
  ]
}
"""


def judge_cluster_promotions(
    *,
    completion: JsonCompletionPort,
    clusters: list[dict[str, str]],
) -> dict[str, str]:
    """근거가 덧붙은 클러스터마다 쌓인 근거만 보고 승격 여부를 정하고, 승격할 클러스터의 이유를 돌려준다."""
    promoted: dict[str, str] = {}
    for batch in _promotion_batches(clusters):
        try:
            decided = _judge_promotion_batch(completion, batch)
        except (RuntimeError, ValueError, TimeoutError):
            decided = {}
        # 묶음 호출이 실패하거나 빠뜨린 클러스터는 하나씩 다시 묻는다.
        for cluster in batch:
            if cluster["cluster_id"] not in decided:
                decided.update(_judge_promotion_batch(completion, [cluster]))
        promoted.update(
            {cluster_id: item["reason"] for cluster_id, item in decided.items() if item["status"] == "candidate"}
        )
    return promoted


def _promotion_batches(clusters: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    batches: list[list[dict[str, str]]] = []
    size = 0
    for cluster in clusters:
        section = cluster["section"]
        if len(section) > PROMOTION_BATCH_CHARS:
            # ponytail: 너무 커진 클러스터는 머리(이름·대표명)와 최근 근거만 본다. 요약 단계가 필요하면 그때 둔다.
            section = f"{section[:400]}\n...\n{section[-(PROMOTION_BATCH_CHARS - 400):]}"
        cluster = {**cluster, "section": section}
        if batches and size + len(section) <= PROMOTION_BATCH_CHARS:
            batches[-1].append(cluster)
            size += len(section)
        else:
            batches.append([cluster])
            size = len(section)
    return batches


def _judge_promotion_batch(
    completion: JsonCompletionPort,
    batch: list[dict[str, str]],
) -> dict[str, dict[str, str]]:
    raw = completion.complete_json(
        PROMOTION_PROMPT,
        json.dumps(
            {"clusters": [{"cluster_id": item["cluster_id"], "markdown": item["section"]} for item in batch]},
            ensure_ascii=False,
            indent=2,
        ),
    )
    valid_ids = {item["cluster_id"] for item in batch}
    decided: dict[str, dict[str, str]] = {}
    for item in raw.get("decisions") or []:
        if not isinstance(item, dict) or str(item.get("cluster_id")) not in valid_ids:
            continue
        status = "candidate" if item.get("promotion_status") == "candidate" else "none"
        decided[str(item["cluster_id"])] = {"status": status, "reason": str(item.get("reason") or "")}
    return decided


def normalize_concept_update_decisions(
    raw: dict[str, Any],
    *,
    concepts: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    concept_slug_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not concepts or not candidates:
        return []
    valid_candidate_ids = {item["candidate_id"] for item in candidates}
    valid_concept_slugs = {str(concept.get("slug")) for concept in concepts if concept.get("slug")}
    slug_map = concept_slug_map or {}
    normalized_decisions: list[dict[str, Any]] = []
    for item, candidate_id in _valid_candidate_decisions(
        raw,
        valid_candidate_ids,
        key="concept_update_decisions",
    ):
        decision = str(item.get("decision") or "not_same_concept")
        raw_concept_slug = str(item.get("concept_slug") or "")
        concept_slug = slug_map.get(raw_concept_slug, raw_concept_slug)
        relation = str(item.get("relation") or "")
        if relation not in {
            "part_of",
            "child_of",
            "uses_or_depends_on",
            "contrasts_with",
            "supports_or_enables",
            "related_evidence",
            "insufficient_evidence",
        }:
            relation = ""
        if decision == "same_concept" and concept_slug in valid_concept_slugs:
            relation = "same_concept"
        elif decision == "relation_candidate" and concept_slug in valid_concept_slugs and relation:
            pass
        else:
            decision = "not_same_concept"
            concept_slug = ""
            relation = ""
        normalized_decisions.append(
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "concept_slug": concept_slug,
                "relation": relation,
                "reason": item.get("reason"),
            }
        )
    return normalized_decisions


def judge_concept_update_candidates(
    *,
    completion: JsonCompletionPort,
    concepts: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not concepts or not candidates:
        return []
    system_prompt = """Stage=ConceptUpdateCandidateJudge.
You decide whether section/mention evidence claims already belong to or have an evidence-backed relation with an existing/current core concept.

Rules:
- Return JSON only.
- Use same_concept only when the candidate has the same identity as an existing/current concept, such as a synonym, translation, abbreviation, or spelling variant.
- A procedure, record, metric, property, evidence, or subtopic that merely belongs to or supports an existing concept is not same_concept. Use relation_candidate when its relation is grounded; otherwise use not_same_concept.
- Use relation_candidate when the candidate is not the same concept but has an evidence-backed relation to an existing/current concept.
- Use not_same_concept when it should remain available for active cluster judging.
- Do not create new concepts or clusters.
- Allowed relation values: part_of, child_of, uses_or_depends_on, contrasts_with, supports_or_enables, related_evidence, insufficient_evidence.

Schema:
{
  "decisions": [
    {
      "candidate_id": "cand_001",
      "decision": "same_concept | relation_candidate | not_same_concept",
      "concept_slug": "existing-concept-slug-or-empty",
      "relation": "part_of | child_of | uses_or_depends_on | contrasts_with | supports_or_enables | related_evidence | insufficient_evidence | empty",
      "reason": "brief Korean reason"
    }
  ]
}
"""
    payload = {
        "concepts": [
            {
                "slug": concept.get("slug"),
                "title": concept.get("title"),
                "aliases": concept.get("aliases", []),
                "definition": concept.get("definition", ""),
                "evidence": concept.get("evidence", []),
                "why_page_worthy": concept.get("why_page_worthy", ""),
            }
            for concept in concepts
            if concept.get("slug")
        ],
        "incoming_candidates": [
            {
                "candidate_id": item["candidate_id"],
                "term": item["term"],
                "suggested_slug": item["slug"],
                "claim": item["claim"],
                "refs": item["refs"],
                "candidate_type": item["candidate_type"],
            }
            for item in candidates
        ],
    }
    raw = completion.complete_json(
        system_prompt,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    return normalize_concept_update_decisions(
        {"concept_update_decisions": raw.get("decisions", [])},
        concepts=concepts,
        candidates=candidates,
    )


def _valid_candidate_decisions(
    raw: dict[str, Any],
    valid_candidate_ids: set[str],
    *,
    key: str = "decisions",
) -> list[tuple[dict[str, Any], str]]:
    decisions = raw.get(key, [])
    if not isinstance(decisions, list):
        return []

    valid_decisions = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "")
        if candidate_id in valid_candidate_ids:
            valid_decisions.append((item, candidate_id))
    return valid_decisions
