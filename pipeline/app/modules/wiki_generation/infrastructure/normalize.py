from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Iterable, List

from app.modules.wiki_generation.domain.entities import NormalizedConcept, NormalizedEvidence, SourceBlock, SourceDocument
from app.modules.wiki_generation.domain.text_utils import (
    clean_concept_tags,
    contains_ci,
    mention_source_text,
    name_key,
    slugify,
    unique_keep_order,
)
from app.modules.wiki_generation.domain.concept_identity import available_concept_slug


class PipelineValidationError(Exception):
    pass


class SemanticNormalizer:
    """Turns loose LLM semantic notes into backend-friendly normalized records."""

    def __init__(self, document: SourceDocument, blocks: list[SourceBlock]) -> None:
        self.document = document
        self.blocks = blocks
        self.by_block_id = {b.block_id: b for b in blocks}

    def normalize_notes(self, notes: list[dict[str, Any]]) -> dict[str, Any]:
        warnings: list[str] = []
        normalized_notes = []
        concept_rows: list[NormalizedConcept] = []
        evidence_rows: list[NormalizedEvidence] = []
        categories_by_name: dict[str, dict[str, Any]] = {}
        section_candidates_by_slug: dict[str, dict[str, Any]] = {}
        mentions_by_slug: dict[str, dict[str, Any]] = {}
        observations: list[dict[str, Any]] = []

        for note_idx, note in enumerate(notes):
            chunk_id = note.get("chunk_id") or f"chunk_{note_idx + 1:04d}"
            normalized_note = self._normalize_single_note(note, warnings)
            normalized_notes.append(normalized_note)

            evidence_by_local_id: dict[str, NormalizedEvidence] = {}
            for ev in (note.get("evidence_claims") or []):
                evidence_id = f"ev_{len(evidence_rows) + 1:04d}"
                row = self._normalize_evidence(evidence_id, ev, warnings)
                evidence_rows.append(row)
                local_id = str(ev.get("id") or "").strip()
                if local_id:
                    evidence_by_local_id[local_id] = row

            for observation in (normalized_note.get("observations") or []):
                self._resolve_observation_evidence(observation, evidence_by_local_id, warnings)
            for observation in (normalized_note.get("observations") or []):
                observations.append(
                    {
                        **observation,
                        "observation_id": f"O{len(observations) + 1:03d}",
                        "source_document_id": self.document.document_id,
                    }
                )

            for category in (normalized_note.get("categories") or []):
                self._merge_source_item(categories_by_name, category, key="term")

            for section in (normalized_note.get("section_candidates") or []):
                self._resolve_section_evidence(section, evidence_by_local_id, warnings)
                self._merge_source_item(section_candidates_by_slug, section, key="slug")

            for mention in (normalized_note.get("mentions") or []):
                self._merge_source_item(mentions_by_slug, mention, key="slug")

            concept_inputs = note.get("core_concepts") or (note.get("concept_candidates") or [])
            for rank, c in enumerate(concept_inputs, start=1):
                concept = self._normalize_concept(c, warnings)
                concept.importance_score += max(0, 10 - rank)
                concept_rows.append(concept)

        # slugify 충돌은 동일성이 아니다. 같은 제목만 먼저 합치고, 나머지는 이름 순서로 기존 후보 규칙을 적용한다.
        concepts_by_title: dict[str, NormalizedConcept] = {}
        for concept in sorted(concept_rows, key=lambda item: (name_key(item.title), item.slug, item.title)):
            title_key = name_key(concept.title)
            if title_key in concepts_by_title:
                self._merge_concept(concepts_by_title[title_key], concept)
                concepts_by_title[title_key].importance_score += concept.importance_score * 0.5
            else:
                concepts_by_title[title_key] = concept
        concepts_by_slug: dict[str, NormalizedConcept] = {}
        for concept in sorted(concepts_by_title.values(), key=lambda item: (name_key(item.title), item.slug)):
            concept.slug = available_concept_slug(concept.slug, concepts_by_slug)
            concepts_by_slug[concept.slug] = concept

        # 근거 힌트 원문을 이름 키로 맞춘다. `C#`을 먼저 slugify해 `C` 근거로 바꾸지 않는다.
        slugs_by_name: dict[str, set[str]] = {}
        for concept in concepts_by_slug.values():
            for name in [concept.title, *concept.identity_aliases]:
                slugs_by_name.setdefault(name_key(name), set()).add(concept.slug)
        for ev in evidence_rows:
            ev.related_concept_slugs = unique_keep_order(
                next(iter(matches)) if len(matches := slugs_by_name.get(name_key(hint), set())) == 1 else slugify(hint)
                for hint in ev.related_concept_slugs
            )

        # Attach evidence to concepts.
        missing_related_hints: dict[str, dict[str, Any]] = {}
        for ev in evidence_rows:
            for slug in ev.related_concept_slugs:
                if slug in concepts_by_slug:
                    concepts_by_slug[slug].evidence_claim_ids.append(ev.evidence_id)
                    concepts_by_slug[slug].importance_score += 2 * max(0.0, min(1.0, ev.confidence))
                else:
                    item = missing_related_hints.setdefault(
                        slug,
                        {
                            "slug": slug,
                            "evidence_ids": [],
                            "sample_claims": [],
                            "max_confidence": 0.0,
                        },
                    )
                    item["evidence_ids"].append(ev.evidence_id)
                    if len(item["sample_claims"]) < 2:
                        item["sample_claims"].append(ev.claim)
                    item["max_confidence"] = max(item["max_confidence"], ev.confidence)

        alias_concept_links = _split_concept_aliases(concepts_by_slug)

        # Backend expands direct mentions from all blocks by title/aliases.
        for concept in concepts_by_slug.values():
            mention_refs = self._expand_mentions(concept)
            concept.mention_reference_ids = unique_keep_order(concept.anchor_reference_ids + mention_refs)
            concept.mention_count = len(concept.mention_reference_ids)
            concept.display_reference_ids = concept.anchor_reference_ids[:3] or concept.mention_reference_ids[:3]
            concept.source_document_ids = unique_keep_order(concept.source_document_ids + [self.document.document_id])
            concept.importance_score += concept.mention_count * 0.25

        return {
            "document": asdict(self.document),
            "semantic_notes": normalized_notes,
            "concept_ledger": [asdict(c) for c in sorted(concepts_by_slug.values(), key=lambda c: (-c.importance_score, c.slug))],
            "categories": sorted(categories_by_name.values(), key=lambda x: x.get("name", "")),
            "section_candidates": sorted(section_candidates_by_slug.values(), key=lambda x: x.get("slug", "")),
            "mentions": [
                {**mention, "definition": self._mention_definition(mention)}
                for mention in sorted(mentions_by_slug.values(), key=lambda x: x.get("slug", ""))
            ],
            "observations": observations,
            "evidence_units": [asdict(e) for e in evidence_rows],
            "alias_concept_links": alias_concept_links,
            "missing_related_concept_hints": sorted(missing_related_hints.values(), key=lambda x: (-x["max_confidence"], x["slug"])),
            "warnings": warnings,
        }

    def _normalize_single_note(self, note: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
        return {
            "chunk_id": note.get("chunk_id"),
            "semantic_summary": note.get("semantic_summary", ""),
            "key_points": [
                {
                    "text": kp.get("text", ""),
                    "anchor_reference_ids": self._anchor_refs((kp.get("anchor_block_ids") or []), warnings, limit=3),
                }
                for kp in (note.get("key_points") or [])
            ],
            "observations": [
                {
                    "type": _observation_type(obs.get("type")),
                    "title": str(obs.get("title", "")).strip(),
                    "query_text": _optional_text(obs.get("query_text")),
                    "summary": str(obs.get("summary", "")).strip(),
                    "evidence_refs": [str(ref).strip() for ref in (obs.get("evidence_refs") or []) if str(ref).strip()],
                    # 다른 노트의 중복 관찰을 합칠 때 옮겨 온 근거 문장이다. 추출 출력에는 보통 없다.
                    "claims": [str(claim).strip() for claim in (obs.get("claims") or []) if str(claim).strip()],
                    "related_concept_hints": [slugify(x) for x in (obs.get("related_concept_hints") or [])],
                    "anchor_reference_ids": self._anchor_refs((obs.get("anchor_block_ids") or []), warnings, limit=5),
                }
                for obs in (note.get("observations") or [])
                if str(obs.get("title", "")).strip() or str(obs.get("summary", "")).strip()
            ],
            "categories": [
                {
                    "term": str(item.get("name", "")).strip(),
                    "name": str(item.get("name", "")).strip(),
                    "slug": slugify(item.get("slug_hint") or item.get("name", "")),
                    "anchor_reference_ids": self._anchor_refs(
                        (item.get("evidence_block_ids") or []) or (item.get("anchor_block_ids") or []), warnings, limit=3
                    ),
                }
                for item in (note.get("categories") or [])
                if str(item.get("name", "")).strip()
            ],
            "core_concepts": [
                {
                    "term": c.get("title", ""),
                    "title": c.get("title", ""),
                    "slug": slugify(c.get("slug_hint") or c.get("title", "")),
                    "anchor_reference_ids": self._anchor_refs(
                        (c.get("evidence_block_ids") or []) or (c.get("anchor_block_ids") or []), warnings, limit=3
                    ),
                }
                for c in (note.get("core_concepts") or (note.get("concept_candidates") or []))
            ],
            "section_candidates": [
                {
                    "term": item.get("title", ""),
                    "title": item.get("title", ""),
                    "slug": slugify(item.get("slug_hint") or item.get("title", "")),
                    "context": item.get("context") or item.get("summary", ""),
                    "evidence_refs": [str(ref).strip() for ref in (item.get("evidence_refs") or []) if str(ref).strip()],
                    "anchor_reference_ids": self._anchor_refs(
                        (item.get("evidence_block_ids") or []) or (item.get("anchor_block_ids") or []), warnings, limit=3
                    ),
                }
                for item in (note.get("section_candidates") or [])
                if item.get("title") or item.get("slug_hint")
            ],
            "mentions": [
                {
                    "term": item.get("name", ""),
                    "name": item.get("name", ""),
                    "slug": slugify(item.get("slug_hint") or item.get("name", "")),
                    "anchor_reference_ids": self._anchor_refs(
                        (item.get("evidence_block_ids") or []) or (item.get("anchor_block_ids") or []), warnings, limit=3
                    ),
                }
                for item in (note.get("mentions") or [])
                if item.get("name") or item.get("slug_hint")
            ],
            "concept_candidates": [
                {
                    "title": c.get("title", ""),
                    "slug": slugify(c.get("slug_hint") or c.get("title", "")),
                    "anchor_reference_ids": self._anchor_refs(
                        (c.get("evidence_block_ids") or []) or (c.get("anchor_block_ids") or []), warnings, limit=3
                    ),
                }
                for c in (note.get("core_concepts") or (note.get("concept_candidates") or []))
            ],
            "evidence_claims": [
                {
                    "claim": ev.get("claim", ""),
                    "anchor_reference_ids": self._anchor_refs((ev.get("anchor_block_ids") or []), warnings, limit=3),
                    "related_concept_hints": [slugify(x) for x in (ev.get("related_concept_hints") or [])],
                    "confidence": ev.get("confidence", 0.0),
                }
                for ev in (note.get("evidence_claims") or [])
            ],
            "needs_neighbor_context": bool(note.get("needs_neighbor_context", False)),
            "context_problem": note.get("context_problem"),
        }

    def _resolve_observation_evidence(
        self,
        observation: dict[str, Any],
        evidence_by_local_id: dict[str, NormalizedEvidence],
        warnings: list[str],
    ) -> None:
        """관찰이 가리킨 근거 주장으로 claims를 채운다. 같은 사실을 관찰과 근거에 두 번 쓰지 않게 하려는 구조다.

        근거 id(ev_NNNN)는 실행마다 다시 매겨져 관찰에 남기지 않는다. 남기면 재편입 병합의 중복 판정이 깨진다.
        """
        rows = []
        for ref in observation.pop("evidence_refs", []):
            row = evidence_by_local_id.get(ref)
            if row is None:
                warnings.append(f"unknown observation evidence_ref: {ref}")
                continue
            rows.append(row)
        observation["claims"] = unique_keep_order([*observation.get("claims", []), *(row.claim for row in rows)])
        observation["anchor_reference_ids"] = unique_keep_order(
            observation["anchor_reference_ids"] + [ref for row in rows for ref in row.anchor_reference_ids]
        )

    def _resolve_section_evidence(
        self,
        section: dict[str, Any],
        evidence_by_local_id: dict[str, NormalizedEvidence],
        warnings: list[str],
    ) -> None:
        """섹션 용어가 가리킨 근거 주장을 전역 근거 id로 잇는다. 하위 항목으로 배정되면 이 근거가 항목 근거가 된다(ADR 0042).

        같은 블록에 섹션이 여럿이어도 근거의 주인을 블록 겹침이 아니라 이 연결로 정한다.
        """
        rows = []
        for ref in section.pop("evidence_refs", []):
            row = evidence_by_local_id.get(ref)
            if row is None:
                warnings.append(f"unknown section evidence_ref: {ref}")
                continue
            rows.append(row)
        section["definition"] = str(section.get("context") or "")
        section["evidence_claim_ids"] = [row.evidence_id for row in rows]
        section["anchor_reference_ids"] = unique_keep_order(
            section["anchor_reference_ids"] + [ref for row in rows for ref in row.anchor_reference_ids]
        )

    def _merge_source_item(self, bucket: dict[str, dict[str, Any]], item: dict[str, Any], key: str) -> None:
        raw_key = str(item.get(key, "")).strip()
        if not raw_key:
            return
        merge_key = raw_key.lower() if key in {"name", "term"} else raw_key
        existing = bucket.setdefault(merge_key, {**item, "anchor_reference_ids": []})
        # 근거 주장 연결은 섹션 용어만 가진다.
        for field in ("anchor_reference_ids", "evidence_claim_ids"):
            if field in item:
                existing[field] = unique_keep_order((existing.get(field) or []) + item[field])
        for field in ("context", "definition", "why_page_worthy"):
            if item.get(field) and len(str(item.get(field))) > len(str(existing.get(field, ""))):
                existing[field] = item[field]

    def _mention_definition(self, mention: dict[str, Any]) -> str:
        """첫 근거 블록의 언급 원문. 근거 블록이 없으면 정의가 없다."""
        block = next((self.by_block_id[ref] for ref in mention["anchor_reference_ids"] if ref in self.by_block_id), None)
        return mention_source_text(str(mention.get("name") or ""), block.text) if block else ""

    def _anchor_refs(self, anchor_block_ids: Iterable[str], warnings: list[str], limit: int = 3) -> list[str]:
        refs = []
        for bid in anchor_block_ids or []:
            if bid not in self.by_block_id:
                warnings.append(f"unknown anchor_block_id: {bid}")
                continue
            refs.append(bid)
            if len(refs) >= limit:
                break
        return unique_keep_order(refs)

    def _normalize_concept(self, c: dict[str, Any], warnings: list[str]) -> NormalizedConcept:
        title = c.get("title") or "Untitled Concept"
        slug = slugify(c.get("slug_hint") or title)
        anchor_refs = self._anchor_refs((c.get("evidence_block_ids") or []) or (c.get("anchor_block_ids") or []), warnings, limit=3)
        aliases = unique_keep_order([str(a).strip() for a in (c.get("aliases") or []) if str(a).strip()] + [title])
        identity_aliases = unique_keep_order(
            str(a).strip() for a in (c.get("aliases") or []) if str(a).strip() and name_key(a) != name_key(title)
        )
        return NormalizedConcept(
            slug=slug,
            title=title,
            aliases=aliases,
            identity_aliases=identity_aliases,
            tags=clean_concept_tags(c.get("tags") or [], title, aliases),
            definition=c.get("definition", ""),
            why_page_worthy=c.get("why_page_worthy", ""),
            anchor_reference_ids=anchor_refs,
            source_document_ids=[self.document.document_id],
        )

    def _merge_concept(self, target: NormalizedConcept, incoming: NormalizedConcept) -> None:
        target.aliases = unique_keep_order(target.aliases + incoming.aliases)
        target.identity_aliases = unique_keep_order(target.identity_aliases + incoming.identity_aliases)
        target.tags = clean_concept_tags(target.tags + incoming.tags, target.title, target.aliases)
        if len(incoming.definition) > len(target.definition):
            target.definition = incoming.definition
        if len(incoming.why_page_worthy) > len(target.why_page_worthy):
            target.why_page_worthy = incoming.why_page_worthy
        target.anchor_reference_ids = unique_keep_order(target.anchor_reference_ids + incoming.anchor_reference_ids)
        target.source_document_ids = unique_keep_order(target.source_document_ids + incoming.source_document_ids)

    def _normalize_evidence(self, evidence_id: str, ev: dict[str, Any], warnings: list[str]) -> NormalizedEvidence:
        refs = self._anchor_refs((ev.get("anchor_block_ids") or []), warnings, limit=3)
        hints = [str(value).strip() for value in (ev.get("related_concept_hints") or []) if str(value).strip()]
        confidence = ev.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except Exception:
            confidence = 0.0
        return NormalizedEvidence(
            evidence_id=evidence_id,
            claim=ev.get("claim", ""),
            anchor_reference_ids=refs,
            related_concept_slugs=unique_keep_order(hints),
            confidence=max(0.0, min(1.0, confidence)),
            source_document_id=self.document.document_id,
        )

    def _expand_mentions(self, concept: NormalizedConcept) -> list[str]:
        # Exact-ish lexical expansion. This is backend calculation, not LLM output.
        # In production, this can be replaced with hybrid search + optional LLM yes/no verifier.
        needles = unique_keep_order([concept.title] + concept.aliases)
        refs = []
        for b in self.blocks:
            if any(self._direct_mention(b.text, n) for n in needles):
                refs.append(b.block_id)
        return unique_keep_order(refs)

    def _direct_mention(self, text: str, needle: str) -> bool:
        needle = needle.strip()
        if not needle:
            return False
        # For Korean aliases, substring is acceptable. For English, require substring
        # across case; this keeps the prototype simple.
        return contains_ci(text, needle)


def _split_concept_aliases(concepts_by_slug: dict[str, NormalizedConcept]) -> list[dict[str, str]]:
    """별칭이 같은 문서의 다른 핵심 개념 이름이면 별칭에서 빼고 두 개념을 잇는다.

    다른 핵심 개념이 된 이름은 같은 개념의 다른 이름이 아니라 따로 둔 하위·관련 개념이다. 별칭에 남기면
    나중에 그 이름의 개념이 들어올 때 이름 일치로 잘못 병합된다.
    """
    owner_by_name = {}
    for concept in concepts_by_slug.values():
        for name in (concept.title,):
            owner_by_name.setdefault(name_key(name), concept.slug)
    identity_names_by_slug = {
        slug: {name_key(alias) for alias in concept.identity_aliases}
        for slug, concept in concepts_by_slug.items()
    }
    links = []
    for concept in concepts_by_slug.values():
        kept = []
        for alias in concept.aliases:
            owner = owner_by_name.get(name_key(alias))
            mutual = owner is not None and name_key(concept.title) in identity_names_by_slug[owner]
            if owner and owner != concept.slug and not mutual:
                links.append({"source": concept.slug, "target": owner})
                continue
            kept.append(alias)
        concept.aliases = kept
        kept_keys = {name_key(alias) for alias in kept}
        concept.identity_aliases = [alias for alias in concept.identity_aliases if name_key(alias) in kept_keys]
    return links


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _observation_type(value: Any) -> str:
    allowed = {
        "source_claim",
        "definition",
        "comparison",
        "example",
        "qa_episode",
        "follow_up",
        "correction",
        "decision",
    }
    text = str(value or "").strip()
    return text if text in allowed else "source_claim"
