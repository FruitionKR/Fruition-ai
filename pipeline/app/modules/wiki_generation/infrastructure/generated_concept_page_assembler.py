from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.modules.wiki_generation.domain.entities import SourceBlock
from app.modules.wiki_generation.domain.text_utils import slugify, unique_keep_order
from app.modules.wiki_generation.infrastructure.ref_format import (
    cite_refs,
    display_ref as _display_ref,
    global_refs as _global_refs,
)
from app.modules.wiki_ingestion.infrastructure.file_io import write_text


class GeneratedConceptPageAssembler:
    """Assemble concept pages from ConceptPageGeneration API outputs."""

    def _map_block_ids(self, anchor_block_ids: list[str], source_blocks: list[SourceBlock], warnings: list[str], context: str) -> list[str]:
        by_bid = {b.block_id: b.block_id for b in source_blocks}
        refs = []
        for bid in anchor_block_ids or []:
            if bid not in by_bid:
                warnings.append(f"{context}: unknown concept-page anchor_block_id {bid}")
                continue
            refs.append(by_bid[bid])
        return unique_keep_order(refs)

    def normalize_generated_output(
        self,
        concept: dict[str, Any],
        raw_page: dict[str, Any],
        source_blocks: list[SourceBlock],
        warnings: list[str],
    ) -> dict[str, Any]:
        definition = raw_page.get("definition", {})
        if isinstance(definition, str):
            warnings.append(f"{concept['slug']}: definition should be object; accepted string fallback")
            definition_obj = {"text": re.sub(r"\s*\[B\d{4}\](?:,\s*\[B\d{4}\])*\s*", "", definition).strip(), "anchor_block_ids": []}
        elif isinstance(definition, dict):
            definition_obj = definition
        else:
            definition_obj = {"text": "", "anchor_block_ids": []}
        normalized = {
            "slug": concept["slug"],
            "title": raw_page.get("title") or concept.get("title"),
            "definition": {
                "text": definition_obj.get("text", ""),
                "anchor_reference_ids": self._map_block_ids(definition_obj.get("anchor_block_ids", []), source_blocks, warnings, f"{concept['slug']}.definition"),
            },
            "key_points": [],
            "evidence": [],
            "related_concept_hints": raw_page.get("related_concept_hints", []),
            "confidence": raw_page.get("confidence", 0.0),
            "source_document_ids": concept.get("source_document_ids", []),
            "mention_count": concept.get("mention_count", 0),
            "importance_score": concept.get("importance_score", 0),
            "aliases": concept.get("aliases", []),
            "display_reference_ids": concept.get("display_reference_ids", []),
        }
        for i, kp in enumerate(raw_page.get("key_points", []) or [], start=1):
            normalized["key_points"].append(
                {
                    "text": kp.get("text", ""),
                    "anchor_reference_ids": self._map_block_ids(kp.get("anchor_block_ids", []), source_blocks, warnings, f"{concept['slug']}.key_points[{i}]"),
                }
            )
        for i, ev in enumerate(raw_page.get("evidence", []) or [], start=1):
            normalized["evidence"].append(
                {
                    "text": ev.get("text", ""),
                    "anchor_reference_ids": self._map_block_ids(ev.get("anchor_block_ids", []), source_blocks, warnings, f"{concept['slug']}.evidence[{i}]"),
                }
            )
        return normalized

    def build_pages(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        built_pages = []
        for page in pages:
            document_id = next(iter(page.get("source_document_ids", []) or []), None)
            definition = page.get("definition", {})
            kp_lines = [
                f"- {kp.get('text', '')}{cite_refs(kp.get('anchor_reference_ids', []), document_id)}"
                for kp in page.get("key_points", [])
                if kp.get("text")
            ]
            ev_lines = [
                f"- {ev.get('text', '')}{cite_refs(ev.get('anchor_reference_ids', []), document_id)}"
                for ev in page.get("evidence", [])
                if ev.get("text")
            ]
            rel_lines = []
            for hint in page.get("related_concept_hints", []):
                rel_lines.append(f"- [[{slugify(str(hint))}|{hint}]]")
            md = f"""---
type: concept
slug: {page['slug']}
sources: {', '.join(page.get('source_document_ids', []))}
mention_count: {page.get('mention_count', 0)}
importance_score: {round(page.get('importance_score', 0), 2)}
generated_by: llm_concept_page_generation
confidence: {page.get('confidence', 0.0)}
---

# {page.get('title') or page['slug']}

## Definition
{definition.get('text') or '정의 없음.'}{cite_refs(definition.get('anchor_reference_ids', []), document_id)}

## Key Points
{chr(10).join(kp_lines) if kp_lines else '- 핵심 포인트 없음'}

## Aliases
{', '.join(page.get('aliases', [])) or '-'}

## Evidence
{chr(10).join(ev_lines) if ev_lines else '- 근거 없음'}

## Related Concepts
{chr(10).join(rel_lines) if rel_lines else '- 관련 개념 없음'}

## Reference Summary
- display refs: {', '.join(_display_ref(r) for r in _global_refs(document_id, page.get('display_reference_ids', []))) or '-'}
- mention_count: {page.get('mention_count', 0)}
"""
            built_pages.append(
                {
                    "slug": page["slug"],
                    "title": page.get("title") or page["slug"],
                    "markdown_path": f"wiki/concepts/{page['slug']}.md",
                    "markdown": md,
                }
            )
        return built_pages

    def assemble_pages(self, pages: list[dict[str, Any]], out_dir: str | Path) -> list[str]:
        out_paths = []
        for page in self.build_pages(pages):
            out_path = Path(out_dir) / page["markdown_path"]
            write_text(out_path, page["markdown"])
            out_paths.append(str(out_path))
        return out_paths

    def assemble_generated(self, normalized: dict[str, Any], concept_page_outputs: list[dict[str, Any]], out_dir: str | Path) -> list[str]:
        """Assemble Markdown from actual ConceptPageGeneration JSON outputs."""
        ledger_by_slug = {c.get("slug"): c for c in normalized.get("concept_ledger", [])}
        out_paths = []

        def cite(refs: list[str]) -> str:
            labels = ", ".join(_display_ref(r) for r in _global_refs(document_id, refs or []))
            return f" [{labels}]" if labels else ""

        for draft in concept_page_outputs:
            slug = draft.get("slug")
            ledger = ledger_by_slug.get(slug, {})
            document_id = next(iter(ledger.get("source_document_ids", []) or []), None)
            title = draft.get("title") or ledger.get("title") or slug
            definition = draft.get("definition", {}) or {}
            key_points = draft.get("key_points", []) or []
            evidence = draft.get("evidence", []) or []
            related = draft.get("related_concept_hints", []) or []

            key_lines = []
            for item in key_points:
                key_lines.append(f"- {item.get('text', '').strip()}" + cite(item.get("anchor_reference_ids", [])))

            ev_lines = []
            for item in evidence:
                ev_lines.append(f"- {item.get('text', '').strip()}" + cite(item.get("anchor_reference_ids", [])))

            related_lines = [f"- {x}" for x in related if x]
            aliases = ", ".join(ledger.get("aliases", []))
            display_refs = ", ".join(_display_ref(r) for r in _global_refs(document_id, ledger.get("display_reference_ids", [])))
            confidence = draft.get("confidence", "")

            md = f"""---
type: concept
slug: {slug}
sources: {', '.join(ledger.get('source_document_ids', []))}
mention_count: {ledger.get('mention_count', 0)}
importance_score: {round(ledger.get('importance_score', 0), 2)}
llm_confidence: {confidence}
---

# {title}

## Definition
{definition.get('text') or ledger.get('definition') or '정의 없음.'}{cite(definition.get('anchor_reference_ids', []))}

## Key Points
{chr(10).join(key_lines) if key_lines else '- 핵심 포인트 없음'}

## Evidence
{chr(10).join(ev_lines) if ev_lines else '- evidence 없음'}

## Related Concepts
{chr(10).join(related_lines) if related_lines else '- 아직 없음'}

## Backend Metadata
- aliases: {aliases or '-'}
- display refs: {display_refs or '-'}
- mention_count: {ledger.get('mention_count', 0)}
"""
            out_path = Path(out_dir) / "wiki" / "concepts" / f"{slug}.md"
            write_text(out_path, md)
            out_paths.append(str(out_path))
        return out_paths
