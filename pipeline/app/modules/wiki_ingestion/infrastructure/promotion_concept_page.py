from __future__ import annotations

from typing import Any

from app.modules.wiki_generation.domain.text_utils import slugify


def promotion_representative(cluster: dict[str, Any]) -> str:
    return str(cluster.get("representative") or cluster.get("id") or "").strip()


def build_promotion_concept_page(
    cluster: dict[str, Any],
    draft: dict[str, Any],
    allowed_refs: set[str],
    source_ref_by_block: dict[str, str],
) -> dict[str, Any]:
    slug = slugify(str(draft.get("slug") or cluster.get("id") or "promoted-concept"))
    title = promotion_representative(cluster) or slug
    definition = draft.get("definition") if isinstance(draft.get("definition"), dict) else {}
    key_points = draft.get("key_points") if isinstance(draft.get("key_points"), list) else []
    evidence = draft.get("evidence") if isinstance(draft.get("evidence"), list) else []
    related = draft.get("related_concept_hints") if isinstance(draft.get("related_concept_hints"), list) else []
    claim_refs = [ref for claim in cluster.get("claims", []) for ref in claim.get("refs", [])]
    source_docs = sorted({ref.split(":", 1)[0] for ref in claim_refs if ":" in ref})
    evidence_lines = [
        f"- {str(item.get('text') or '').strip()}{cite_lint_refs(normalize_lint_refs(item.get('anchor_block_ids', []), allowed_refs, source_ref_by_block))}"
        for item in evidence
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    if not evidence_lines:
        evidence_lines = [
            f"- {claim.get('id')}: {claim.get('claim') or claim.get('text')}{cite_lint_refs(claim.get('refs', []))}"
            for claim in cluster.get("claims", [])
        ]
    key_point_lines = [
        f"- {str(item.get('text') or '').strip()}{cite_lint_refs(normalize_lint_refs(item.get('anchor_block_ids', []), allowed_refs, source_ref_by_block))}"
        for item in key_points
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    definition_text = str(definition.get("text") or "").strip() or (cluster.get("claims", [{}])[0].get("claim") or "정의 없음.")
    definition_refs = normalize_lint_refs(definition.get("anchor_block_ids", []), allowed_refs, source_ref_by_block) or claim_refs[:3]
    related_lines = [f"- [[{slugify(str(item))}|{item}]]" for item in related if item]
    for relation in cluster.get("relations", []):
        target = str(relation.get("target") or "")
        if target.startswith("concept:"):
            target_slug = target.split(":", 1)[1]
            line = f"- [[{target_slug}|{target_slug}]]"
            if line not in related_lines:
                related_lines.append(line)
    md = f"""---
type: concept
slug: {slug}
sources: {', '.join(source_docs)}
mention_count: {len(cluster.get('claims', []))}
importance_score: 0.7
generated_by: llm_promotion_materialization
llm_confidence: {draft.get('confidence', 0.0)}
---

# {title}

## Definition
{definition_text}{cite_lint_refs(definition_refs)}

## Why It Matters
{draft.get('why_it_matters') or 'Promotion cluster에서 독립 concept 후보로 판단된 항목이다.'}

## Key Points
{chr(10).join(key_point_lines) if key_point_lines else '- 핵심 포인트 없음'}

## Aliases
-

## Evidence
{chr(10).join(evidence_lines) if evidence_lines else '- evidence 없음'}

## Related Concepts
{chr(10).join(related_lines) if related_lines else '- 관련 개념 없음'}

## Reference Summary
- display refs: {', '.join(sorted(claim_refs)) or '-'}
- promoted_from: cluster:{cluster.get('id')}
"""
    return {"slug": slug, "title": title, "markdown": md}


def normalize_lint_refs(value: Any, allowed_refs: set[str], source_ref_by_block: dict[str, str]) -> list[str]:
    refs = []
    raw_refs = value if isinstance(value, list) else [value]
    for raw in raw_refs:
        ref = str(raw or "").strip()
        if not ref:
            continue
        if ref in allowed_refs:
            refs.append(ref)
        elif ref in source_ref_by_block:
            refs.append(source_ref_by_block[ref])
    return list(dict.fromkeys(refs))


def cite_lint_refs(refs: list[str]) -> str:
    clean = [str(ref) for ref in refs if ref]
    return f" [{', '.join(clean)}]" if clean else ""
