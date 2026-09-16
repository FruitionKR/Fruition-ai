from app.modules.query.application.ports import EmbeddingSearchPort


class StaticScoreEmbeddingSearch(EmbeddingSearchPort):
    def __init__(self, scores_by_title: dict[str, float] | None = None) -> None:
        self._scores_by_title = scores_by_title or {}

    def score(self, query: str, documents: list[str]) -> list[float]:
        return [self._score_document(document) for document in documents]

    def _score_document(self, document: str) -> float:
        first_line = document.splitlines()[0] if document.splitlines() else document
        return self._scores_by_title.get(first_line, 0.0)

