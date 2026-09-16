import re
from dataclasses import dataclass

from app.modules.query.application.evidence_text import (
    clean_sentence,
    specificity_bonus,
    split_paragraphs,
    split_sentences,
    split_structured_evidence_units,
    tokens,
)
from app.modules.query.application.ports import EmbeddingSearchPort, TextSearchPort
from app.modules.query.application.source_references import (
    legacy_source_fields,
    remove_block_refs,
    source_references,
    source_references_from_ids,
)
from app.modules.query.domain.entities import EvidenceSnippet, RetrievedPage, SourceReference, WikiEmbeddingUnit, WikiPage
from app.modules.query.domain.scoring import hybrid_score


@dataclass(frozen=True)
class _EvidenceCandidate:
    page_id: str
    source_document_id: str
    source_block_ids: list[str]
    source_refs: list[SourceReference]
    text: str
    score: float
    unit_type: str = "evidence"


def has_selectable_evidence(
    page: WikiPage,
    embedding_units: list[WikiEmbeddingUnit],
) -> bool:
    if embedding_units:
        return any(
            unit.source_block_ids
            and unit.text
            and source_references_from_ids(unit.source_block_ids, unit.source_document_id)
            for unit in embedding_units
        )
    source_document_id = _source_document_id_for_page(page)
    return bool(source_references(page.markdown or page.summary, source_document_id))


def _source_document_id_for_page(page: WikiPage) -> str | None:
    if page.source_document_id:
        return page.source_document_id
    markdown = page.markdown or ""
    if page.is_source:
        return _frontmatter_value(markdown, "document_id") or _source_id_from_page_id(page.id)
    if page.is_concept:
        sources = _frontmatter_value(markdown, "sources")
        if not sources:
            return None
        source_ids = [part.strip() for part in re.split(r"[,\s]+", sources) if part.strip()]
        if len(source_ids) == 1:
            return source_ids[0]
    return None


def _source_id_from_page_id(page_id: str) -> str | None:
    if page_id.startswith("source:"):
        return page_id.split(":", 1)[1]
    return None


def _frontmatter_value(markdown: str, key: str) -> str | None:
    if not markdown.startswith("---"):
        return None
    parts = markdown.split("---", 2)
    if len(parts) < 3:
        return None
    for line in parts[1].splitlines():
        match = re.match(rf"^{re.escape(key)}\s*:\s*(.+)$", line.strip(), flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


class EvidenceSelector:
    def __init__(
        self,
        embedding_search: EmbeddingSearchPort | None = None,
        text_search: TextSearchPort | None = None,
        max_related_pages: int = 8,
        max_paragraphs_per_page: int = 4,
        evidence_embedding_weight: float = 0.75,
        min_evidence_score: float = 0.0,
        evidence_relative_score_floor: float = 0.85,
        max_evidence_snippets: int = 8,
    ) -> None:
        self._embedding_search = embedding_search
        self._text_search = text_search
        self._max_related_pages = max_related_pages
        self._max_paragraphs_per_page = max_paragraphs_per_page
        self._evidence_embedding_weight = evidence_embedding_weight
        self._min_evidence_score = min_evidence_score
        self._evidence_relative_score_floor = evidence_relative_score_floor
        self._max_evidence_snippets = max(1, max_evidence_snippets)

    def select(
        self,
        question: str,
        related_pages: list[RetrievedPage],
        embedding_units_by_page_id: dict[str, list[WikiEmbeddingUnit]],
    ) -> list[EvidenceSnippet]:
        candidates: list[_EvidenceCandidate] = []
        for item in related_pages[: self._max_related_pages]:
            source_document_id = self._source_document_id(item)
            if item.page.page_type == "web":
                candidates.extend(self._score_web_evidence(question, item))
                continue
            stored_units = embedding_units_by_page_id.get(item.page.id, [])
            if stored_units:
                candidates.extend(self._score_stored_embedding_units(question, item, stored_units))
                continue
            if not has_selectable_evidence(item.page, stored_units):
                continue
            candidates.extend(self._score_evidence_sentences(question, item, source_document_id or ""))

        all_candidates = self._dedupe_evidence(candidates)
        candidates = self._select_evidence_score_band_by_page(all_candidates)
        candidates = self._include_atomic_units_for_selected_refs(all_candidates, candidates)
        candidates.sort(key=lambda snippet: snippet.score, reverse=True)
        candidates = candidates[: self._max_evidence_snippets]
        return [
            EvidenceSnippet(
                rank=index,
                source_document_id=snippet.source_document_id,
                source_block_ids=snippet.source_block_ids,
                source_refs=snippet.source_refs,
                text=snippet.text,
            )
            for index, snippet in enumerate(candidates, start=1)
        ]

    def _select_evidence_score_band_by_page(self, candidates: list[_EvidenceCandidate]) -> list[_EvidenceCandidate]:
        if not candidates:
            return []
        by_page: dict[str, list[_EvidenceCandidate]] = {}
        for candidate in candidates:
            by_page.setdefault(candidate.page_id, []).append(candidate)
        selected: list[_EvidenceCandidate] = []
        for page_candidates in by_page.values():
            page_candidates.sort(key=lambda candidate: candidate.score, reverse=True)
            top_score = page_candidates[0].score
            if top_score <= 0:
                selected.extend(candidate for candidate in page_candidates if candidate.score >= self._min_evidence_score)
                continue
            floor = max(self._min_evidence_score, top_score * self._evidence_relative_score_floor)
            selected.extend(candidate for candidate in page_candidates if candidate.score >= floor)
        return self._dedupe_evidence(selected)

    def _include_atomic_units_for_selected_refs(
        self,
        all_candidates: list[_EvidenceCandidate],
        selected: list[_EvidenceCandidate],
    ) -> list[_EvidenceCandidate]:
        selected_keys = {self._candidate_key(candidate) for candidate in selected}
        selected_refs_by_page: dict[str, set[str]] = {}
        for candidate in selected:
            if len(candidate.source_block_ids) <= 1:
                continue
            selected_refs_by_page.setdefault(candidate.page_id, set()).update(candidate.source_block_ids)

        expanded = list(selected)
        for candidate in all_candidates:
            if len(candidate.source_block_ids) != 1:
                continue
            if candidate.source_block_ids[0] not in selected_refs_by_page.get(candidate.page_id, set()):
                continue
            key = self._candidate_key(candidate)
            if key in selected_keys:
                continue
            selected_keys.add(key)
            expanded.append(candidate)
        return expanded

    def _score_evidence_sentences(
        self,
        question: str,
        item: RetrievedPage,
        source_document_id: str,
    ) -> list[_EvidenceCandidate]:
        content = item.page.markdown or item.page.summary
        paragraphs = split_paragraphs(content)
        if not paragraphs:
            return []

        raw_candidates: list[_EvidenceCandidate] = []
        for paragraph in paragraphs:
            for unit, unit_type, unit_weight in split_structured_evidence_units(paragraph):
                source_refs = source_references(unit, source_document_id)
                if not source_refs:
                    continue
                legacy_document_id, legacy_block_ids = legacy_source_fields(source_refs, source_document_id)
                clean_sentence = remove_block_refs(unit)
                raw_candidates.append(
                    _EvidenceCandidate(
                        page_id=item.page.id,
                        source_document_id=legacy_document_id,
                        source_block_ids=legacy_block_ids,
                        source_refs=source_refs,
                        text=clean_sentence,
                        score=item.score * unit_weight,
                        unit_type=unit_type,
                    )
                )

        scored = self._score_structured_candidates(question, raw_candidates, item)
        scored.sort(key=lambda value: -value.score)
        return self._select_evidence_candidates(scored)

    def _score_stored_embedding_units(
        self,
        question: str,
        item: RetrievedPage,
        units: list[WikiEmbeddingUnit],
    ) -> list[_EvidenceCandidate]:
        raw_candidates: list[_EvidenceCandidate] = []
        for unit in units:
            if not unit.source_block_ids or not unit.text:
                continue
            source_refs = source_references_from_ids(unit.source_block_ids, unit.source_document_id)
            if not source_refs:
                continue
            legacy_document_id, legacy_block_ids = legacy_source_fields(source_refs, unit.source_document_id)
            raw_candidates.append(
                _EvidenceCandidate(
                    page_id=unit.page_id,
                    source_document_id=legacy_document_id,
                    source_block_ids=legacy_block_ids,
                    source_refs=source_refs,
                    text=unit.text,
                    score=item.score * unit.weight,
                    unit_type=unit.unit_type,
                )
            )
        return self._score_structured_candidates(question, raw_candidates, item)

    def _select_evidence_candidates(self, candidates: list[_EvidenceCandidate]) -> list[_EvidenceCandidate]:
        ranked = self._dedupe_evidence(candidates)
        selected: list[_EvidenceCandidate] = []
        selected_keys: set[tuple[str, tuple[tuple[str, str], ...]]] = set()

        atomic_by_ref: dict[str, _EvidenceCandidate] = {}
        for candidate in ranked:
            if len(candidate.source_block_ids) != 1:
                continue
            ref = candidate.source_block_ids[0]
            current = atomic_by_ref.get(ref)
            if current is None or candidate.score > current.score:
                atomic_by_ref[ref] = candidate

        priority_atomic: list[_EvidenceCandidate] = []
        priority_keys: set[tuple[str, tuple[str, ...]]] = set()
        for candidate in ranked:
            if len(candidate.source_block_ids) <= 1:
                continue
            for ref in candidate.source_block_ids:
                atomic = atomic_by_ref.get(ref)
                if atomic is None:
                    continue
                key = self._candidate_key(atomic)
                if key in priority_keys:
                    continue
                priority_keys.add(key)
                priority_atomic.append(atomic)

        for candidate in [*priority_atomic, *sorted(atomic_by_ref.values(), key=lambda value: -value.score)]:
            if len(selected) >= self._max_paragraphs_per_page:
                break
            self._append_selected_evidence(selected, selected_keys, candidate)

        for candidate in ranked:
            if len(selected) >= self._max_paragraphs_per_page:
                break
            self._append_selected_evidence(selected, selected_keys, candidate)

        selected.sort(key=lambda value: -value.score)
        return selected

    def _append_selected_evidence(
        self,
        selected: list[_EvidenceCandidate],
        selected_keys: set[tuple[str, tuple[tuple[str, str], ...]]],
        candidate: _EvidenceCandidate,
    ) -> None:
        key = self._candidate_key(candidate)
        if key in selected_keys:
            return
        selected_keys.add(key)
        selected.append(candidate)

    def _score_web_evidence(self, question: str, item: RetrievedPage) -> list[_EvidenceCandidate]:
        text = item.page.markdown or item.page.summary
        if not text:
            return []
        query_terms = set(tokens(question))
        units = split_sentences(text)
        if not units:
            units = [clean_sentence(text)]
        scored: list[_EvidenceCandidate] = []
        for unit in units:
            clean_unit = remove_block_refs(unit)
            if not clean_unit:
                continue
            unit_terms = set(tokens(clean_unit))
            overlap = len(query_terms & unit_terms)
            score = overlap * 2.0 + item.score
            if overlap == 0:
                score -= 0.50
            scored.append(
                _EvidenceCandidate(
                    page_id=item.page.id,
                    source_document_id=item.page.id,
                    source_block_ids=["web"],
                    source_refs=[],
                    text=clean_unit,
                    score=score,
                )
            )
        scored.sort(key=lambda value: -value.score)
        return self._dedupe_evidence(scored)[: self._max_paragraphs_per_page]

    def _score_structured_candidates(
        self,
        question: str,
        candidates: list[_EvidenceCandidate],
        item: RetrievedPage,
    ) -> list[_EvidenceCandidate]:
        if not candidates:
            return []

        texts = [candidate.text for candidate in candidates]
        if self._embedding_search and self._text_search:
            embedding_scores = self._embedding_search.score(question, texts)
            text_scores = self._text_search.score(question, texts)
            query_terms = set(tokens(question))
            scored = []
            for candidate, embedding_score, text_score in zip(candidates, embedding_scores, text_scores):
                evidence_score = hybrid_score(
                    embedding_score,
                    text_score,
                    embedding_weight=self._evidence_embedding_weight,
                )
                overlap = len(query_terms & set(tokens(candidate.text)))
                lexical_bonus = overlap * 0.20
                if item.role == "focus_concept":
                    lexical_bonus += 0.25
                elif overlap > 0 and item.role == "seed_source":
                    lexical_bonus += 0.25
                candidate_specificity_bonus = specificity_bonus(candidate.text)
                scored.append(
                    _EvidenceCandidate(
                        page_id=candidate.page_id,
                        source_document_id=candidate.source_document_id,
                        source_block_ids=candidate.source_block_ids,
                        source_refs=candidate.source_refs,
                        text=candidate.text,
                        score=item.score + candidate.score + evidence_score + lexical_bonus + candidate_specificity_bonus,
                        unit_type=candidate.unit_type,
                    )
                )
            return scored

        query_terms = set(tokens(question))
        scored = []
        for candidate in candidates:
            overlap = len(query_terms & set(tokens(candidate.text)))
            score = candidate.score + item.score + overlap * 2.0
            if item.role == "focus_concept":
                score += 0.5
            elif overlap > 0 and item.role == "seed_source":
                score += 0.5
            if overlap == 0:
                score -= 0.75
            score += specificity_bonus(candidate.text)
            scored.append(
                _EvidenceCandidate(
                    page_id=candidate.page_id,
                    source_document_id=candidate.source_document_id,
                    source_block_ids=candidate.source_block_ids,
                    source_refs=candidate.source_refs,
                    text=candidate.text,
                    score=score,
                    unit_type=candidate.unit_type,
                )
            )
        return scored

    def _dedupe_evidence(self, candidates: list[_EvidenceCandidate]) -> list[_EvidenceCandidate]:
        deduped: list[_EvidenceCandidate] = []
        seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for candidate in candidates:
            key = self._candidate_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

    def _candidate_key(self, candidate: _EvidenceCandidate) -> tuple[str, tuple[tuple[str, str], ...]]:
        if candidate.source_refs:
            return (
                candidate.text,
                tuple((ref.source_document_id, ref.source_block_id) for ref in candidate.source_refs),
            )
        return (
            candidate.text,
            tuple((candidate.source_document_id, block_id) for block_id in candidate.source_block_ids),
        )

    def _source_document_id(self, item: RetrievedPage) -> str | None:
        return _source_document_id_for_page(item.page)
