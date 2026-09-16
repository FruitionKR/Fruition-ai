import re

from app.modules.query.application.ports import EmbeddingSearchPort, TextSearchPort
from app.modules.query.domain.entities import QueryRewrite, WikiPage
from app.modules.query.domain.scoring import hybrid_score


class QueryPageScorer:
    def __init__(
        self,
        embedding_search: EmbeddingSearchPort,
        text_search: TextSearchPort,
        source_candidate_limit: int = 15,
        concept_candidate_limit: int = 10,
        focus_concept_threshold: float = 0.45,
        score_source_structures: bool = True,
    ) -> None:
        self._embedding_search = embedding_search
        self._text_search = text_search
        self._source_candidate_limit = source_candidate_limit
        self._concept_candidate_limit = concept_candidate_limit
        self._focus_concept_threshold = focus_concept_threshold
        self._score_source_structures_enabled = score_source_structures

    def score_pages(self, query_rewrite: QueryRewrite, pages: list[WikiPage], embedding_weight: float) -> dict[str, float]:
        embedding_query = (
            query_rewrite.retrieval_query
            if self._embedding_search is self._text_search
            else query_rewrite.original_question
        )
        text_query = query_rewrite.retrieval_query
        representations = [self._representation(page) for page in pages]
        embedding_scores = self._embedding_search.score(embedding_query, representations)
        text_scores = (
            self._text_search.score(text_query, representations)
            if embedding_weight < 1.0
            else [0.0 for _ in representations]
        )
        base_scores = {
            page.id: self._final_retrieval_score(
                hybrid_score(embedding_score, text_score, embedding_weight=embedding_weight),
                self._name_match_score(query_rewrite, page),
            )
            for page, embedding_score, text_score in zip(pages, embedding_scores, text_scores)
        }
        if (
            self._score_source_structures_enabled
            and pages
            and all(page.is_source for page in pages)
        ):
            structure_scores = self._score_source_structures(
                embedding_query,
                text_query,
                pages,
                embedding_weight,
            )
            return {
                page.id: min(1.0, base_scores.get(page.id, 0.0) + structure_scores.get(page.id, 0.0))
                for page in pages
            }
        return base_scores

    def select_seed_sources(self, source_pages: list[WikiPage], source_scores: dict[str, float]) -> list[str]:
        ranked = sorted(source_pages, key=lambda page: source_scores.get(page.id, 0.0), reverse=True)
        if not ranked:
            return []
        top_score = source_scores.get(ranked[0].id, 0.0)
        return [
            page.id
            for page in ranked
            if source_scores.get(page.id, 0.0) >= top_score - 0.02
        ][: self._source_candidate_limit]

    def select_focus_concepts(self, concept_pages: list[WikiPage], concept_scores: dict[str, float]) -> list[str]:
        ranked = sorted(concept_pages, key=lambda page: concept_scores.get(page.id, 0.0), reverse=True)
        if not ranked:
            return []
        top_score = concept_scores.get(ranked[0].id, 0.0)
        if top_score + 1e-9 < self._focus_concept_threshold:
            return []
        focus = [page.id for page in ranked if concept_scores.get(page.id, 0.0) >= top_score - 0.001]
        return focus[: self._concept_candidate_limit]

    def select_direct_match_concepts(self, query_rewrite: QueryRewrite, concept_pages: list[WikiPage]) -> list[str]:
        direct_matches = [
            page.id
            for page in concept_pages
            if self._name_match_score(query_rewrite, page) >= 1.0
        ]
        return direct_matches[: self._concept_candidate_limit]

    def _score_source_structures(
        self,
        embedding_query: str,
        text_query: str,
        pages: list[WikiPage],
        embedding_weight: float,
    ) -> dict[str, float]:
        weighted_representations: list[tuple[str, str, float]] = []
        for page in pages:
            for representation, weight in self._source_structure_representations(page):
                weighted_representations.append((page.id, representation, weight))
        if not weighted_representations:
            return {}

        documents = [item[1] for item in weighted_representations]
        embedding_scores = self._embedding_search.score(embedding_query, documents)
        text_scores = (
            self._text_search.score(text_query, documents)
            if embedding_weight < 1.0
            else [0.0 for _ in documents]
        )
        scores: dict[str, float] = {}
        for (page_id, _, weight), embedding_score, text_score in zip(weighted_representations, embedding_scores, text_scores):
            score = hybrid_score(embedding_score, text_score, embedding_weight=embedding_weight) * weight
            scores[page_id] = max(scores.get(page_id, 0.0), score)
        return scores

    def _source_structure_representations(self, page: WikiPage) -> list[tuple[str, float]]:
        markdown = page.markdown or ""
        sections = self._markdown_sections(markdown)
        weighted_sections = [
            ("Categories", 0.10),
            ("Core Concepts", 0.20),
            ("Section Candidates", 0.25),
            ("Mentions", 0.15),
        ]
        representations = []
        for section_name, weight in weighted_sections:
            body = sections.get(section_name.lower())
            if body:
                representations.append((f"{section_name}\n{body}", weight))
        return representations

    def _markdown_sections(self, markdown: str) -> dict[str, str]:
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in markdown.splitlines():
            match = re.match(r"^##\s+(.+?)\s*$", line.strip())
            if match:
                current = match.group(1).strip().lower()
                sections.setdefault(current, [])
                continue
            if current is not None:
                sections[current].append(line)
        return {
            section: "\n".join(lines).strip()
            for section, lines in sections.items()
            if "\n".join(lines).strip()
        }

    def _final_retrieval_score(self, retrieval_score: float, name_match_score: float) -> float:
        if name_match_score >= 1.0:
            return max(retrieval_score, 0.95)
        if name_match_score >= 0.85:
            return max(retrieval_score, 0.88)
        if name_match_score > 0:
            return min(1.0, retrieval_score + 0.20 * name_match_score)
        return retrieval_score

    def _name_match_score(self, query_rewrite: QueryRewrite, page: WikiPage) -> float:
        query_names = self._query_name_variants(query_rewrite)
        page_names = self._page_name_variants(page)
        if not query_names or not page_names:
            return 0.0

        for query_name in query_names:
            if query_name in page_names:
                return 1.0

        if page.is_source:
            return 0.0

        for query_name in query_names:
            for page_name in page_names:
                if len(query_name) >= 2 and len(page_name) >= 2 and (query_name in page_name or page_name in query_name):
                    return 0.85
        return 0.0

    def _query_name_variants(self, query_rewrite: QueryRewrite) -> set[str]:
        values = [query_rewrite.original_question, query_rewrite.retrieval_query, *query_rewrite.keywords]
        variants = set()
        for value in values:
            for token in re.findall(r"[A-Za-z0-9가-힣_.-]+", value.lower()):
                normalized = self._normalize_name(token)
                if normalized and normalized not in self._question_stopwords():
                    variants.add(normalized)
        return variants

    def _page_name_variants(self, page: WikiPage) -> set[str]:
        values = [page.title, page.slug, *self._extract_aliases(page.markdown or "")]
        return {normalized for value in values if (normalized := self._normalize_name(value))}

    def _extract_aliases(self, markdown: str) -> list[str]:
        aliases = []
        in_alias_block = False
        for line in markdown.splitlines():
            stripped = line.strip()
            if re.match(r"^aliases\s*:\s*$", stripped, flags=re.IGNORECASE):
                in_alias_block = True
                continue
            if in_alias_block:
                if stripped.startswith("-"):
                    aliases.append(stripped.lstrip("-").strip().strip('"').strip("'"))
                    continue
                in_alias_block = False
            match = re.match(r"^(aliases|alias|별칭|다른 이름)\s*[:：]\s*(.+)$", stripped, flags=re.IGNORECASE)
            if match:
                aliases.extend(part.strip().strip('"').strip("'") for part in re.split(r"[,/、，]", match.group(2)) if part.strip())
        return aliases

    def _normalize_name(self, value: str) -> str:
        normalized = value.lower().strip()
        normalized = re.sub(r"\([^)]*\)", "", normalized)
        normalized = re.sub(r"[^a-z0-9가-힣]+", "", normalized)
        if re.search(r"[가-힣]", normalized):
            for suffix in ["에서는", "으로부터", "로부터", "에게서", "한테서", "에게", "한테", "으로", "로", "이랑", "랑", "이나", "나", "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "와", "과"]:
                if normalized.endswith(suffix):
                    return normalized[: -len(suffix)] if len(normalized) > len(suffix) + 1 else normalized
        return normalized

    def _question_stopwords(self) -> set[str]:
        return {
            "뭐",
            "뭐야",
            "무엇",
            "어떻게",
            "왜",
            "차이",
            "설명",
            "알려줘",
            "what",
            "is",
            "are",
            "the",
            "a",
            "an",
        }

    def _representation(self, page: WikiPage) -> str:
        markdown = page.markdown or ""
        return "\n".join([page.title, page.summary, markdown]).strip()
