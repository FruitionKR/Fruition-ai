import os
from typing import Any

from app.modules.query.application.ports import EmbeddingSearchPort


class BgeM3EmbeddingSearch(EmbeddingSearchPort):
    def __init__(self, model_name: str | None = None, batch_size: int = 16) -> None:
        self._model_name = model_name or os.environ.get("EMBEDDING_MODEL_NAME") or "BAAI/bge-m3"
        self._batch_size = batch_size
        self._model: Any | None = None

    def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        model = self._load_model()
        embeddings = model.encode(
            [query, *documents],
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        query_embedding = embeddings[0]
        document_embeddings = embeddings[1:]
        return [self._dot(query_embedding, document_embedding) for document_embedding in document_embeddings]

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "BGE-M3 embedding search requires sentence-transformers. "
                "Install llmPipeline requirements before enabling query embedding search."
            ) from exc

        self._model = SentenceTransformer(self._model_name)
        return self._model

    def _dot(self, left: Any, right: Any) -> float:
        value = float(sum(float(a) * float(b) for a, b in zip(left, right)))
        return max(0.0, min(1.0, value))
