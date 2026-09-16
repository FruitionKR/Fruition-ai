import math
from collections import Counter

from app.modules.query.application.evidence_text import tokens
from app.modules.query.application.ports import TextSearchPort


class Bm25Searcher(TextSearchPort):
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b

    def score(self, query: str, documents: list[str]) -> list[float]:
        query_terms = self._tokens(query)
        if not query_terms or not documents:
            return [0.0 for _ in documents]

        tokenized_documents = [self._tokens(document) for document in documents]
        document_lengths = [len(tokens) for tokens in tokenized_documents]
        average_length = sum(document_lengths) / len(document_lengths) if document_lengths else 0.0
        document_frequency = self._document_frequency(tokenized_documents)
        document_count = len(tokenized_documents)
        raw_scores = [
            self._score_document(
                query_terms,
                tokens,
                document_frequency,
                average_length,
                document_count,
            )
            for tokens in tokenized_documents
        ]
        return self._normalize(raw_scores)

    def _score_document(
        self,
        query_terms: list[str],
        document_terms: list[str],
        document_frequency: dict[str, int],
        average_length: float,
        document_count: int,
    ) -> float:
        if not document_terms:
            return 0.0

        counts = Counter(document_terms)
        document_count = max(1, document_count)
        document_length = len(document_terms)
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if frequency == 0:
                continue
            df = document_frequency.get(term, 0)
            idf = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
            denominator = frequency + self._k1 * (1 - self._b + self._b * document_length / max(average_length, 1.0))
            score += idf * (frequency * (self._k1 + 1)) / denominator
        return score

    def _document_frequency(self, tokenized_documents: list[list[str]]) -> dict[str, int]:
        frequency: dict[str, int] = {}
        for tokens in tokenized_documents:
            for term in set(tokens):
                frequency[term] = frequency.get(term, 0) + 1
        return frequency

    def _normalize(self, scores: list[float]) -> list[float]:
        maximum = max(scores) if scores else 0.0
        if maximum <= 0:
            return [0.0 for _ in scores]
        return [score / maximum for score in scores]

    def _tokens(self, text: str) -> list[str]:
        return tokens(text)
