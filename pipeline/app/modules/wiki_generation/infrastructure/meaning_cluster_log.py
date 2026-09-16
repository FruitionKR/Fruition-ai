from __future__ import annotations

from typing import Any

from app.modules.wiki_generation.infrastructure.ref_format import cite_global_refs


def render_meaning_cluster_log(
    normalized: dict[str, Any],
    clusters: list[dict[str, Any]],
    user_id: str,
    workspace_id: str,
    concept_update_decisions: list[dict[str, Any]],
    invalid_candidates: list[dict[str, Any]],
    ingest_date: str,
) -> str:
    doc = normalized.get("document", {})
    document_id = doc.get("document_id") or "unknown"
    lines = [
        f"## {ingest_date} ingest: {document_id}",
        "",
        f"user: {user_id}",
        f"workspace: {workspace_id}",
        f"input: {doc.get('source_path') or document_id}",
        f"created_source_page: source:{document_id}",
        "",
        "### Extracted Claims",
    ]
    claim_rows = [claim for cluster in clusters for claim in cluster.get("evidence_claims", [])]
    if claim_rows:
        lines.extend(f"- {claim['id']}: {claim['claim']}{cite_global_refs(claim.get('refs', []))}" for claim in claim_rows)
    else:
        lines.append("- extracted claim 없음")

    lines.extend(["", "### Invalid Candidates"])
    if invalid_candidates:
        for item in invalid_candidates:
            lines.append(f"- {item.get('claim_id')} ({item.get('candidate_type')}): {item.get('claim')}")
            lines.append(f"  reason: {item.get('reason')}")
    else:
        lines.append("- invalid candidate 없음")

    lines.extend(["", "### Concept Update Decisions"])
    if concept_update_decisions:
        for item in concept_update_decisions:
            lines.append(f"- {item.get('claim_id') or item.get('candidate_id')} -> concept:{item.get('concept_slug')}")
            lines.append("  decision: same_concept")
            lines.append(f"  reason: {item.get('reason') or '-'}")
    else:
        lines.append("- concept update decision 없음")

    lines.extend(["", "### Cluster Decisions"])
    if claim_rows:
        for cluster in clusters:
            for claim in cluster.get("evidence_claims", []):
                lines.append(f"- {claim['id']} -> cluster:{cluster['id']}")
                lines.append(f"  decision: {claim.get('cluster_decision') or 'new_cluster'}")
                lines.append(f"  reason: {claim.get('decision_reason') or '-'}")
    else:
        lines.append("- cluster decision 없음")

    lines.extend(["", "### Relation Candidates"])
    relation_rows = [
        (cluster, relation)
        for cluster in clusters
        for relation in cluster.get("core_relation_candidates", [])
    ]
    if relation_rows:
        for cluster, relation in relation_rows:
            lines.append(f"- cluster:{cluster['id']} -> {relation.get('target')}")
            lines.append(f"  relation: {relation.get('relation')}")
            lines.append(f"  evidence: [{', '.join(relation.get('evidence', []))}]")
            lines.append(f"  reason: {relation.get('reason') or '-'}")
    else:
        lines.append("- relation candidate 없음")

    lines.extend(["", "### Promotion Decisions"])
    promotions = [cluster for cluster in clusters if cluster.get("promotion")]
    if promotions:
        for cluster in promotions:
            promotion = cluster["promotion"]
            lines.append(f"- cluster:{cluster['id']}")
            lines.append(f"  status: {promotion.get('status')}")
            lines.append(f"  source_refs: [{', '.join(promotion.get('source_refs', []))}]")
            lines.append(f"  reason: {promotion.get('reason') or '-'}")
    else:
        lines.append("- promotion decision 없음")

    lines.extend(["", "### Materialized Changes", "- updated: clusters/active.md", "- updated: logs/{yyyy-mm-dd}.md"])
    return "\n".join(lines) + "\n"
