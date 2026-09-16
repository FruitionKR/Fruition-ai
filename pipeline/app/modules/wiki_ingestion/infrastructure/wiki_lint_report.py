from __future__ import annotations

from typing import Any


def render_lint_log_markdown(result: dict[str, Any], lint_date: str) -> str:
    lines = [
        f"## {lint_date} lint: {result['workspace_id']}",
        "",
        f"user: {result['user_id']}",
        f"workspace: {result['workspace_id']}",
        f"active: {result['active_path']}",
        "",
        "### Orphan Risks",
    ]
    orphan_refs = result.get("orphan_refs", [])
    lines.extend(f"- {ref}" for ref in orphan_refs) if orphan_refs else lines.append("- orphan risk 없음")
    lines.extend(["", "### Promotion Queue"])
    promotions = result.get("promotion_candidates", [])
    lines.extend(f"- cluster:{cluster_id}" for cluster_id in promotions) if promotions else lines.append("- promotion candidate 없음")
    lines.extend(["", "### Needs Review"])
    needs_review = result.get("needs_review", [])
    lines.extend(f"- cluster:{cluster_id}" for cluster_id in needs_review) if needs_review else lines.append("- needs_review 없음")
    lines.extend(["", "### Relation Candidate Queue"])
    relation_candidates = result.get("relation_candidates", [])
    if relation_candidates:
        for item in relation_candidates:
            lines.append(f"- cluster:{item.get('cluster_id')} -> {item.get('target')}")
            lines.append(f"  relation: {item.get('relation')}")
            lines.append(f"  evidence: [{', '.join(item.get('evidence', []))}]")
    else:
        lines.append("- relation candidate 없음")
    lines.extend(["", "### Invalid Relation Candidates"])
    invalid_relations = result.get("invalid_relations", [])
    if invalid_relations:
        for item in invalid_relations:
            lines.append(f"- cluster:{item.get('cluster_id')} -> {item.get('target') or '-'}")
            lines.append(f"  relation: {item.get('relation') or '-'}")
            lines.append(f"  evidence: [{', '.join(item.get('evidence', []))}]")
            lines.append(f"  missing: [{', '.join(item.get('missing', []))}]")
    else:
        lines.append("- invalid relation candidate 없음")
    lines.extend(["", "### Orphan Wiki Links"])
    orphan_link_candidates = result.get("orphan_link_candidates", [])
    removed_orphan_links = result.get("removed_orphan_links", [])
    if orphan_link_candidates:
        removed_keys = {
            (
                item.get("source"),
                item.get("target"),
                item.get("relation"),
            )
            for item in removed_orphan_links
        }
        for item in orphan_link_candidates:
            key = (
                item.get("source"),
                item.get("target"),
                item.get("relation"),
            )
            decision = "removed" if key in removed_keys else "candidate"
            lines.append(
                f"- {decision}: {item.get('source')} "
                f"-[{item.get('relation')}]-> {item.get('target')}"
            )
            lines.append(f"  reason: {item.get('reason')}")
    else:
        lines.append("- orphan wiki link 없음")
    lines.extend(["", "### Invalid Promotions"])
    invalid_promotions = result.get("invalid_promotions", [])
    if invalid_promotions:
        for item in invalid_promotions:
            lines.append(f"- cluster:{item.get('cluster_id')}")
            lines.append(f"  reason: {item.get('reason')}")
    else:
        lines.append("- invalid promotion 없음")
    lines.extend(["", "### Reingest Reconciliation"])
    reconciliation_candidates = result.get("reconciliation_candidates", [])
    if reconciliation_candidates:
        for item in reconciliation_candidates:
            lines.append(f"- document:{item.get('document_id')}")
            lines.append(
                "  invalidated_source_refs: "
                f"[{', '.join(item.get('invalidated_source_refs', []))}]"
            )
            lines.append(
                "  stale_concept_slugs: "
                f"[{', '.join(item.get('stale_concept_slugs', []))}]"
            )
    else:
        lines.append("- reconciliation candidate 없음")
    lines.extend(["", "### Materialized Changes"])
    materialized_promotions = result.get("materialized_promotions", [])
    merged_promotions = result.get("merged_promotions", [])
    materialized_relations = result.get("materialized_relations", [])
    applied_reconciliations = result.get("applied_reconciliations", [])
    applied_cluster_reconciliation = result.get(
        "applied_cluster_reconciliation",
        {},
    )
    if materialized_promotions:
        for item in materialized_promotions:
            lines.append(f"- promoted: cluster:{item.get('cluster_id')} -> concept:{item.get('concept_slug')}")
    if merged_promotions:
        for item in merged_promotions:
            lines.append(f"- merged: cluster:{item.get('cluster_id')} -> concept:{item.get('concept_slug')}")
    if materialized_relations:
        for item in materialized_relations:
            lines.append(f"- linked: concept:{item.get('from')} -[{item.get('relation')}]-> concept:{item.get('to')}")
    if applied_reconciliations:
        for item in applied_reconciliations:
            lines.append(
                f"- reconciled: document:{item.get('document_id')}"
            )
    for item in applied_cluster_reconciliation.get("removed_claims", []):
        lines.append(
            f"- removed stale claim: cluster:{item.get('cluster_id')} "
            f"claim:{item.get('claim_id')}"
        )
    for item in applied_cluster_reconciliation.get("removed_relations", []):
        lines.append(
            f"- removed stale relation: cluster:{item.get('cluster_id')} "
            f"-> {item.get('target')}"
        )
    lines.append("- updated: logs/{yyyy-mm-dd}.md")
    return "\n".join(lines) + "\n"
