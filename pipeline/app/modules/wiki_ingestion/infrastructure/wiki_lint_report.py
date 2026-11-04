from __future__ import annotations

from collections import Counter
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
    lines.extend(["", "### Concept Sources"])
    unsourced = [item for item in result.get("entry_decisions", []) if item.get("action") == "prune_unsourced"]
    repairs = result.get("source_link_repairs", [])
    lines.extend(f"- 출처 없음 삭제: concept:{item.get('slug')}" for item in unsourced)
    lines.extend(f"- 원문 다시 연결: concept:{item.get('slug')}" for item in repairs)
    if not unsourced and not repairs:
        lines.append("- 출처 이상 없음")
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
    lines.extend(["", "### Retrieval Quality"])
    quality_candidates = result.get("quality_review_candidates", [])
    if quality_candidates:
        counts = Counter(str(item.get("failure_type")) for item in quality_candidates)
        lines.append("- " + ", ".join(f"{name}: {count}" for name, count in sorted(counts.items())))
        for item in quality_candidates:
            lines.append(f"- {item.get('failure_type')}: document:{item.get('document_id')}")
            lines.append(f"  question: {item.get('question')}")
            lines.append(f"  gold_source_refs: [{', '.join(item.get('gold_source_refs', []))}]")
            lines.append(f"  reason: {item.get('reason')}")
    else:
        lines.append("- retrieval quality failure 없음")
    lines.extend(["", "### Materialized Changes"])
    materialized_promotions = result.get("materialized_promotions", [])
    merged_promotions = result.get("merged_promotions", [])
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
    lines.append("- updated: logs/{yyyy-mm-dd}.md")
    return "\n".join(lines) + "\n"
