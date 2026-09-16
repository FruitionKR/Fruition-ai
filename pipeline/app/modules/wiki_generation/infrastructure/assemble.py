from __future__ import annotations

from pathlib import Path
from typing import Any

from app.modules.wiki_ingestion.infrastructure.file_io import write_json, write_text
from app.modules.wiki_generation.domain.text_utils import slugify, unique_keep_order
from app.modules.wiki_generation.infrastructure.concept_page_sections import (
    collect_source_key_points as _collect_source_key_points,
    concept_evidence as _concept_evidence,
    concept_key_points_from_source as _concept_key_points_from_source,
    concept_related_lines as _concept_related_lines,
    item_refs as _item_refs,
)
from app.modules.wiki_generation.infrastructure.generated_concept_page_assembler import GeneratedConceptPageAssembler
from app.modules.wiki_generation.infrastructure.meaning_cluster_artifact import MeaningClusterArtifactAssembler
from app.modules.wiki_generation.infrastructure.ref_format import (
    cite_refs,
    display_ref as _display_ref,
    global_refs as _global_refs,
)


class SourcePageAssembler:
    def build(self, normalized: dict[str, Any], polish: dict[str, Any] | None = None) -> dict[str, Any]:
        doc = normalized["document"]
        notes = normalized["semantic_notes"]
        ledger = normalized["concept_ledger"]
        polish = polish or {}
        title = str(polish.get("title") or doc.get("title") or doc["document_id"]).strip()

        summary_parts = [n.get("semantic_summary", "") for n in notes if n.get("semantic_summary")]
        summary = "\n\n".join(summary_parts[:4]) or "요약 없음."
        if polish.get("summary", {}).get("text"):
            summary = polish["summary"]["text"]

        key_points = []
        seen = set()
        polished_key_points = polish.get("key_points", {}).get("items", [])
        if polished_key_points:
            for kp in polished_key_points:
                text = kp.get("text", "").strip()
                if text:
                    key_points.append(f"- {text}{cite_refs(kp.get('anchor_reference_ids', []), doc['document_id'])}")
        else:
            for n in notes:
                for kp in n.get("key_points", []):
                    text = kp.get("text", "").strip()
                    if not text or text in seen:
                        continue
                    seen.add(text)
                    key_points.append(f"- {text}{cite_refs(kp.get('anchor_reference_ids', []), doc['document_id'])}")

        concept_lines = []
        for c in ledger:
            concept_lines.append(f"- [[{c['slug']}|{c['title']}]]{cite_refs(c.get('display_reference_ids', []), doc['document_id'])}")

        category_lines = [
            f"- {item.get('name')}"
            for item in normalized.get("categories", [])
            if item.get("name")
        ]
        section_lines = [
            _source_section_line(item, doc["document_id"])
            for item in normalized.get("section_candidates", [])
            if item.get("title") or item.get("slug")
        ]
        mention_lines = [
            _source_mention_line(item, doc["document_id"])
            for item in normalized.get("mentions", [])
            if item.get("name") or item.get("slug")
        ]
        observation_lines = [
            _source_observation_line(item, doc["document_id"])
            for item in normalized.get("observations", [])
            if item.get("title") or item.get("summary")
        ]

        md = f"""---
type: source
document_id: {doc['document_id']}
source_file: {doc['source_path']}
categories: {', '.join(item.get('name', '') for item in normalized.get('categories', []) if item.get('name')) or '-'}
---

# {title}

## Summary
{summary}

## Key Points
{chr(10).join(key_points) if key_points else '- 핵심 포인트 없음'}

## Observations
{chr(10).join(observation_lines) if observation_lines else '- observation 없음'}

## Categories
{chr(10).join(category_lines) if category_lines else '- 카테고리 없음'}

## Core Concepts
{chr(10).join(concept_lines) if concept_lines else '- core concept 없음'}

## Section Candidates
{chr(10).join(section_lines) if section_lines else '- section candidate 없음'}

## Mentions
{chr(10).join(mention_lines) if mention_lines else '- mention 없음'}
"""
        filename_slug = slugify(title)
        if filename_slug == "untitled":
            filename_slug = doc["document_id"]
        markdown_path = f"wiki/sources/{filename_slug}.md"
        artifact = _source_extraction_artifact(normalized, title, summary, markdown_path, polish=polish)
        normalized["source_extraction_artifact"] = artifact
        return {
            "slug": filename_slug,
            "title": title,
            "markdown_path": markdown_path,
            "markdown": md,
            "source_extraction_artifact": artifact,
        }

    def assemble(self, normalized: dict[str, Any], out_dir: str | Path, polish: dict[str, Any] | None = None) -> str:
        page = self.build(normalized, polish=polish)
        out_path = _unique_source_path(Path(out_dir) / "wiki" / "sources", page["slug"])
        write_text(out_path, page["markdown"])
        artifact_path = out_path.with_suffix(".json")
        write_json(artifact_path, page["source_extraction_artifact"])
        normalized["source_extraction_artifact"] = str(artifact_path)
        return str(out_path)


def _term_record(item: dict[str, Any]) -> dict[str, Any]:
    term = item.get("term") or item.get("title") or item.get("name") or item.get("slug") or ""
    refs = unique_keep_order(item.get("anchor_reference_ids", []) or item.get("evidence_block_ids", []) or [])
    record = {
        "term": term,
        "slug": item.get("slug") or slugify(str(term)),
    }
    context = item.get("context") or item.get("definition") or ""
    if context:
        record["context"] = context
    if refs:
        record["evidence_block_ids"] = refs
    if item.get("aliases"):
        record["aliases"] = item.get("aliases")
    return record


def _source_extraction_artifact(
    normalized: dict[str, Any],
    title: str,
    summary: str,
    markdown_path: str,
    polish: dict[str, Any] | None = None,
) -> dict[str, Any]:
    doc = normalized["document"]
    core_concepts = [_term_record({**concept, "term": concept.get("title")}) for concept in normalized.get("concept_ledger", [])]
    section_candidates = [_term_record(item) for item in normalized.get("section_candidates", [])]
    mentions = [_term_record(item) for item in normalized.get("mentions", [])]
    categories = unique_keep_order(
        str(item.get("name") or item.get("term") or "").strip()
        for item in normalized.get("categories", [])
        if isinstance(item, dict) and str(item.get("name") or item.get("term") or "").strip()
    )
    return {
        "schema_version": "source-extraction.v1",
        "document_id": doc.get("document_id"),
        "title": title,
        "source_file": doc.get("source_path"),
        "markdown_path": markdown_path,
        "summary": summary,
        "key_points": _source_artifact_key_points(normalized, polish or {}),
        "categories": categories,
        "observations": normalized.get("observations", []),
        "core_concepts": core_concepts,
        "section_candidates": section_candidates,
        "mentions": mentions,
        "evidence_claims": normalized.get("evidence_units", []),
    }


def _source_artifact_key_points(normalized: dict[str, Any], polish: dict[str, Any]) -> list[dict[str, Any]]:
    polished = polish.get("key_points", {}).get("items", [])
    if polished:
        return [
            {"text": item.get("text", ""), "evidence_block_ids": _item_refs(item)}
            for item in polished
            if item.get("text")
        ]
    return [
        {"text": item.get("text", ""), "evidence_block_ids": _item_refs(item)}
        for note in normalized.get("semantic_notes", [])
        for item in note.get("key_points", [])
        if item.get("text")
    ]


def _source_section_line(item: dict[str, Any], document_id: str) -> str:
    title = item.get("title") or item.get("slug") or "section"
    context = item.get("context") or ""
    suffix = f" - {context}" if context else ""
    return f"- {title}{suffix}{cite_refs(item.get('anchor_reference_ids', []), document_id)}"


def _source_observation_line(item: dict[str, Any], document_id: str) -> str:
    observation_id = item.get("observation_id") or "O000"
    observation_type = item.get("type") or "source_claim"
    title = item.get("title") or "observation"
    query_text = item.get("query_text")
    summary = item.get("summary") or ""
    claims = item.get("claims", [])
    related = item.get("related_concept_hints", [])
    parts = [f"{observation_id} ({observation_type}) {title}"]
    if query_text:
        parts.append(f"query: {query_text}")
    if summary:
        parts.append(f"summary: {summary}")
    if claims:
        parts.append(f"claims: {'; '.join(claims[:3])}")
    if related:
        parts.append(f"related: {', '.join(related[:5])}")
    return f"- {' / '.join(parts)}{cite_refs(item.get('anchor_reference_ids', []), document_id)}"


def _source_mention_line(item: dict[str, Any], document_id: str) -> str:
    name = item.get("name") or item.get("slug") or "mention"
    context = item.get("context") or ""
    suffix = f" - {context}" if context else ""
    return f"- {name}{suffix}{cite_refs(item.get('anchor_reference_ids', []), document_id)}"


def _unique_source_path(source_dir: Path, filename_slug: str) -> Path:
    candidate = source_dir / f"{filename_slug}.md"
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        candidate = source_dir / f"{filename_slug}-{i}.md"
        if not candidate.exists():
            return candidate
        i += 1


class ConceptPageAssembler:
    def build_top(
        self,
        normalized: dict[str, Any],
        top_n: int | None = 6,
        polish_by_slug: dict[str, Any] | None = None,
        source_key_points: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        concepts = normalized["concept_ledger"] if top_n is None else normalized["concept_ledger"][:top_n]
        return [
            self._build_page(c, normalized, polish_by_slug or {}, source_key_points)
            for c in concepts
        ]

    def assemble_top(
        self,
        normalized: dict[str, Any],
        out_dir: str | Path,
        top_n: int | None = 6,
        polish_by_slug: dict[str, Any] | None = None,
        source_key_points: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """Deterministic skeleton concept pages.

        This is useful when concept page LLM generation is disabled.
        """
        out_paths = []
        for page in self.build_top(normalized, top_n=top_n, polish_by_slug=polish_by_slug, source_key_points=source_key_points):
            out_path = Path(out_dir) / page["markdown_path"]
            write_text(out_path, page["markdown"])
            out_paths.append(str(out_path))
        return out_paths

    def _build_page(
        self,
        c: dict[str, Any],
        normalized: dict[str, Any],
        polish_by_slug: dict[str, Any] | None = None,
        source_key_points: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        evidence = normalized["evidence_units"]
        document_id = normalized["document"]["document_id"]
        polish_by_slug = polish_by_slug or {}
        ledger_by_slug = {concept["slug"]: concept for concept in normalized["concept_ledger"]}
        source_key_points = source_key_points or _collect_source_key_points(normalized)
        polish = polish_by_slug.get(c["slug"], {})
        related_evidence = _concept_evidence(c, evidence)
        ev_lines = []
        for ev in related_evidence:
            ev_lines.append(f"- {ev['claim']}{cite_refs(ev.get('anchor_reference_ids', []), ev.get('source_document_id') or document_id)}")
        definition_text = c.get("definition") or "정의 초안 없음."
        definition_refs = c.get("display_reference_ids", [])
        if polish.get("definition", {}).get("text"):
            definition_text = polish["definition"]["text"]
            definition_refs = polish["definition"].get("anchor_reference_ids", definition_refs)
        if not ev_lines and c.get("definition"):
            ev_lines.append(f"- {c['definition']}{cite_refs(definition_refs, document_id)}")
        key_point_lines = []
        for item in polish.get("key_points", {}).get("items", []):
            text = item.get("text", "").strip()
            if text:
                key_point_lines.append(f"- {text}{cite_refs(item.get('anchor_reference_ids', []), document_id)}")
        if not key_point_lines:
            key_point_lines = _concept_key_points_from_source(c, related_evidence, source_key_points, document_id)
        if not key_point_lines and c.get("definition"):
            key_point_lines.append(f"- {c['definition']}{cite_refs(definition_refs, document_id)}")
        related_lines = []
        for target_slug in polish.get("related_concept_hints", []):
            target_slug = slugify(str(target_slug))
            target = ledger_by_slug.get(target_slug)
            if target and target_slug != c["slug"]:
                related_lines.append(f"- [[{target_slug}|{target.get('title') or target_slug}]]")
        if not related_lines:
            related_lines = _concept_related_lines(c["slug"], normalized, ledger_by_slug, source_key_points)
        aliases = ", ".join(c.get("aliases", []))
        md = f"""---
type: concept
slug: {c['slug']}
sources: {', '.join(c.get('source_document_ids', []))}
mention_count: {c.get('mention_count', 0)}
importance_score: {round(c.get('importance_score', 0), 2)}
generated_by: backend_skeleton
---

# {c['title']}

## Definition
{definition_text}{cite_refs(definition_refs, document_id)}

## Why It Matters
{c.get('why_page_worthy') or '중요도 설명 없음.'}

## Key Points
{chr(10).join(key_point_lines) if key_point_lines else '- 핵심 포인트 없음'}

## Aliases
{aliases or '-'}

## Evidence
{chr(10).join(ev_lines) if ev_lines else '- 아직 연결된 evidence claim 없음'}

## Related Concepts
{chr(10).join(related_lines) if related_lines else '- 관련 개념 없음'}

## Reference Summary
- display refs: {', '.join(_display_ref(r) for r in _global_refs(document_id, c.get('display_reference_ids', []))) or '-'}
- mention_count: {c.get('mention_count', 0)}
"""
        return {
            "slug": c["slug"],
            "title": c.get("title") or c["slug"],
            "markdown_path": f"wiki/concepts/{c['slug']}.md",
            "markdown": md,
        }


class LinkBuilder:
    def build(self, normalized: dict[str, Any], generated_concept_pages: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        doc = normalized["document"]
        ledger = normalized["concept_ledger"]
        links = []
        source_page_id = f"source:{doc['document_id']}"
        for c in ledger:
            links.append(
                {
                    "source": source_page_id,
                    "target": f"concept:{c['slug']}",
                    "relation": "source_mentions_concept",
                    "decided_by": "backend",
                    }
                )
        return links


class ReviewReport:
    def write(self, normalized: dict[str, Any], out_dir: str | Path, generated_concept_pages: list[dict[str, Any]] | None = None) -> str:
        warnings = normalized.get("warnings", [])
        ledger = normalized.get("concept_ledger", [])
        ev = normalized.get("evidence_units", [])
        lines = [
            "# Fruition v0.9 Pipeline Review",
            "",
            "## Summary",
            f"- concepts: {len(ledger)}",
            f"- evidence claims: {len(ev)}",
            f"- generated concept pages: {len(generated_concept_pages or [])}",
            f"- hint resolutions: {len(normalized.get('hint_resolutions', []))}",
            f"- unresolved related hints: {len(normalized.get('unresolved_related_concept_hints', []))}",
            f"- warnings: {len(warnings)}",
            "",
            "## Top Concepts",
        ]
        for c in ledger[:10]:
            lines.append(f"- {c['title']} (`{c['slug']}`): mention_count={c.get('mention_count')}, score={round(c.get('importance_score', 0), 2)}")
        if generated_concept_pages:
            lines += ["", "## LLM Generated Concept Pages"]
            for p in generated_concept_pages:
                lines.append(f"- {p.get('title')} (`{p.get('slug')}`), confidence={p.get('confidence')}")
        if normalized.get("hint_resolutions"):
            lines += ["", "## Hint Resolutions"]
            for item in normalized.get("hint_resolutions", []):
                lines.append(
                    f"- {item.get('hint_slug')} -> {item.get('canonical_slug') or '-'} "
                    f"({item.get('decision')}), confidence={item.get('confidence')}"
                )
        if normalized.get("unresolved_related_concept_hints"):
            lines += ["", "## Unresolved Related Hints"]
            for item in normalized.get("unresolved_related_concept_hints", []):
                lines.append(f"- {item.get('slug')}: evidence={', '.join(item.get('evidence_ids', []))}")
        lines += ["", "## Warnings"]
        lines.extend([f"- {w}" for w in warnings] or ["- none"])
        path = Path(out_dir) / "review_report.md"
        write_text(path, "\n".join(lines) + "\n")
        return str(path)
