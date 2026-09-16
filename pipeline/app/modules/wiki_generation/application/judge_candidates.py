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
- promotion_status is usually "none".
- Respect the earlier extraction decision: if a term arrived as a
  section_candidate, mention, or evidence item instead of a core_concept, do
  not immediately promote it in the same ingest pass.
- Never set promotion_status to "candidate" for a new_cluster decision. A newly
  created cluster must first remain in active.md and accumulate more evidence.
- Never promote a cluster based on a single incoming candidate, a single claim,
  or a single source in this pass. Promotion requires accumulated evidence that
  already exists in existing_active_clusters plus the incoming claim.
- Use promotion_status "candidate" only when decision is same_cluster and the
  target cluster already exists in existing_active_clusters with multiple
  grounded claims or multiple source refs.
- Use promotion_status "candidate" only when the cluster is worth becoming a
  long-lived core wiki page: it is a reusable domain concept, has grounded
  evidence claims, and can support future relations or retrieval beyond the
  current source.
- Do not promote merely because a term is definable. Definition extractability
  is insufficient without reusable evidence and relation value.
- Do not promote bibliographic/entity metadata: author names, researcher names,
  universities, funders, journals, conferences, publishers, citations, document
  titles, affiliations, or project metadata.
- Do not promote one-off named tools, software, product names, experimental
  labels, parameter labels, or isolated metrics unless the accumulated claims
  show a reusable domain concept with meaningful relations to existing concepts.
- If evidence comes from only one source and mainly identifies a name/entity,
  keep promotion_status as "none".

Schema:
{
  "decisions": [
    {
      "candidate_id": "cand_001",
      "decision": "same_cluster | new_cluster | needs_review",
      "target_cluster_id": "cluster-id-to-update-or-create",
      "representative": "short display label",
      "promotion_status": "none | candidate | needs_review",
      "reason": "brief Korean reason"
    }
  ]
}
"""
    payload = {
        "existing_active_clusters": existing_active_markdown[-24000:],
        "incoming_candidates": [
            {
                "candidate_id": item["candidate_id"],
                "term": item["term"],
                "suggested_slug": item["slug"],
                "claim": item["claim"],
                "refs": item["refs"],
                "candidate_type": item["candidate_type"],
                "suggested_promotion_status": item.get("suggested_promotion_status", "none"),
                "suggested_promotion_reason": item.get("suggested_promotion_reason", ""),
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
        promotion_status = str(item.get("promotion_status") or "none")
        if promotion_status not in {"none", "candidate", "needs_review"}:
            promotion_status = "none"
        normalized_decisions.append(
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "target_cluster_id": item.get("target_cluster_id"),
                "representative": item.get("representative"),
                "promotion_status": promotion_status,
                "reason": item.get("reason"),
            }
        )
    return normalized_decisions

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
