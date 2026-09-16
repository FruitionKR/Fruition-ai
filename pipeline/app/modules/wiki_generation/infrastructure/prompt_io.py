from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from app.modules.wiki_generation.domain.entities import SemanticPacket, SourceBlock


def render_semantic_user_prompt(packet: SemanticPacket, source_context: dict[str, Any] | None = None) -> str:
    """User message for ChunkSemanticExtraction.

    The model returns the exact anchors shown in SOURCE BLOCKS.
    """
    context_text = ""
    source_markdown = str((source_context or {}).get("source_markdown") or "").strip()
    if source_markdown:
        context_text = f"""

EXISTING SOURCE PAGE MARKDOWN:
Use this existing source page as background context only. Do not use references from this markdown in anchor_block_ids; anchor_block_ids must come from SOURCE BLOCKS.
```markdown
{source_markdown}
```
"""

    return f"""Stage input: ChunkSemanticExtraction

chunk_id: {packet.chunk_id}
document_id: {packet.document_id}

Read the following source blocks as one semantic packet. Each source block starts with an anchor in square brackets. Use those exact anchor values in anchor_block_ids.
{context_text}

SOURCE BLOCKS:
{packet.text}
""".rstrip() + "\n"


def build_messages(system_prompt: str, user_prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def render_concept_resolution_user_prompt(
    incoming_concepts: list[dict[str, Any]],
    existing_concepts: list[dict[str, Any]],
    missing_related_hints: list[dict[str, Any]] | None = None,
) -> str:
    incoming_json = json.dumps(
        [
            {
                "slug": concept.get("slug"),
                "title": concept.get("title"),
                "aliases": concept.get("aliases", []),
                "definition": concept.get("definition"),
                "why_page_worthy": concept.get("why_page_worthy"),
            }
            for concept in incoming_concepts
        ],
        ensure_ascii=False,
        indent=2,
    )
    existing_json = json.dumps(
        [
            {
                "slug": concept.get("slug"),
                "title": concept.get("title"),
                "aliases": concept.get("aliases", []),
                "summary": concept.get("summary"),
                "path": concept.get("path"),
            }
            for concept in existing_concepts
        ],
        ensure_ascii=False,
        indent=2,
    )
    hints_json = json.dumps(
        [
            {
                "slug": hint.get("slug"),
                "evidence_count": len(hint.get("evidence_ids", [])),
                "sample_claims": hint.get("sample_claims", [])[:2],
                "max_confidence": hint.get("max_confidence", 0.0),
            }
            for hint in (missing_related_hints or [])
        ],
        ensure_ascii=False,
        indent=2,
    )
    return f"""Stage input: ConceptResolution

Resolve each incoming concept against the existing wiki concept index.
Also resolve missing related concept hints from evidence claims. Missing hints
are not necessarily page-worthy, but they may be synonyms of current concepts,
existing wiki concepts, or related-only link targets.

INCOMING CONCEPTS:
{incoming_json}

EXISTING CONCEPT INDEX:
{existing_json}

MISSING RELATED CONCEPT HINTS:
{hints_json}
""".rstrip() + "\n"


def render_section_polish_user_prompt(payload: dict[str, Any], source_blocks: Sequence[SourceBlock]) -> str:
    evidence_json = json.dumps(
        [
            {
                "claim": ev.get("claim"),
                "anchor_block_ids": ev.get("anchor_reference_ids", []),
                "confidence": ev.get("confidence"),
            }
            for ev in payload.get("evidence", [])
        ],
        ensure_ascii=False,
        indent=2,
    )
    draft_json = json.dumps(payload.get("draft", {}), ensure_ascii=False, indent=2)
    context_json = json.dumps(payload.get("context", {}), ensure_ascii=False, indent=2)
    block_lines = "\n".join(b.to_llm_line() for b in source_blocks)
    return f"""Stage input: SectionPolish

Polish only the requested section. Return JSON only.
If PAGE TYPE is source, also return a concise human-readable title that
summarizes the source topic. If PAGE TYPE is concept, keep title empty unless a
title is already supplied in CONTEXT.
For source_summary_and_key_points, write one holistic summary for the whole
source page from existing_source_markdown/existing_source_summary and current
SOURCE BLOCKS. Do not append a new summary after the old summary.

SECTION:
{payload.get("section")}

PAGE TYPE:
{payload.get("page_type")}

CONTEXT:
{context_json}

DRAFT:
{draft_json}

EVIDENCE CLAIMS:
{evidence_json}

SOURCE BLOCKS:
{block_lines}
""".rstrip() + "\n"


def _unique(items: Iterable[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _blocks_by_ref_map(blocks: Sequence[SourceBlock]) -> dict[str, SourceBlock]:
    by_ref = {b.block_id: b for b in blocks}
    by_ref.update({b.source_reference_id: b for b in blocks})
    return by_ref


def collect_concept_source_blocks(
    concept: dict[str, Any],
    evidence_units: list[dict[str, Any]],
    blocks: Sequence[SourceBlock],
    max_blocks: int | None = None,
    source_key_points: list[dict[str, Any]] | None = None,
) -> list[SourceBlock]:
    """Select blocks for optional ConceptPageGeneration prompt.

    This is deterministic: use display/anchor refs and linked evidence refs
    first, then mention refs. max_blocks can be set by callers that need a
    temporary prompt budget, but the default keeps every matched block.
    """
    by_ref = _blocks_by_ref_map(blocks)
    slug = concept.get("slug")
    refs: list[str] = []
    refs.extend(concept.get("display_reference_ids", []))
    refs.extend(concept.get("anchor_reference_ids", []))
    for ev in evidence_units:
        if slug and slug in ev.get("related_concept_slugs", []):
            refs.extend(ev.get("anchor_reference_ids", []))
    concept_refs = set(_unique(refs + concept.get("mention_reference_ids", [])))
    for key_point in source_key_points or []:
        key_point_refs = key_point.get("anchor_reference_ids", []) or key_point.get("anchor_block_ids", [])
        if concept_refs.intersection(key_point_refs):
            refs.extend(key_point_refs)
    refs.extend(concept.get("mention_reference_ids", []))

    selected = []
    for ref in _unique(refs):
        block = by_ref.get(ref)
        if block is not None:
            selected.append(block)
        if max_blocks is not None and len(selected) >= max_blocks:
            break
    return selected


def render_concept_page_user_prompt(concept: dict[str, Any], evidence_units: list[dict[str, Any]], source_blocks: Sequence[SourceBlock]) -> str:
    related_evidence = [ev for ev in evidence_units if concept.get("slug") in ev.get("related_concept_slugs", [])]
    evidence_json = json.dumps(
        [
            {
                "claim": ev.get("claim"),
                "anchor_block_ids": ev.get("anchor_reference_ids", []),
                "confidence": ev.get("confidence"),
            }
            for ev in related_evidence
        ],
        ensure_ascii=False,
        indent=2,
    )
    block_lines = "\n".join(b.to_llm_line() for b in source_blocks)
    concept_json = json.dumps(
        {
            "title": concept.get("title"),
            "slug": concept.get("slug"),
            "aliases": concept.get("aliases", []),
            "definition_draft": concept.get("definition"),
            "why_page_worthy": concept.get("why_page_worthy"),
            "display_block_ids": concept.get("display_reference_ids", []),
            "required_output_note": "definition must be {text, anchor_block_ids}; do not embed [B-id] citations in text fields",
        },
        ensure_ascii=False,
        indent=2,
    )
    return f"""Stage input: ConceptPageGeneration

Write one concept page from the supplied ledger card, evidence claims, and source blocks. Use only the supplied source blocks. Put citations only in anchor_block_ids; do not put [B0001] citations inside text fields.

CONCEPT LEDGER CARD:
{concept_json}

EVIDENCE CLAIMS:
{evidence_json}

SOURCE BLOCKS:
{block_lines}
""".rstrip() + "\n"
