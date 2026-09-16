import re
from collections import Counter

from app.modules.query.application.ports import TextSearchPort


class SimpleTextSearcher(TextSearchPort):
    def score(self, query: str, documents: list[str]) -> list[float]:
        query_terms = set(self._tokens(query))
        if not query_terms:
            return [0.0 for _ in documents]
        scores = []
        for document in documents:
            counts = Counter(self._tokens(document))
            overlap = sum(1 for term in query_terms if counts[term] > 0)
            scores.append(overlap / len(query_terms))
        return scores

    def _tokens(self, text: str) -> list[str]:
        return re.findall(r"[A-Za-z0-9가-힣_.-]+", text.lower())

