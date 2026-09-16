import os
from functools import lru_cache
from typing import Any

from app.modules.wiki_embedding.application.ports import EmbeddingModelPort


class BgeM3EmbeddingModel(EmbeddingModelPort):
    def __init__(self, model_name: str | None = None, batch_size: int = 16) -> None:
        self._model_name = model_name or os.environ.get("EMBEDDING_MODEL_NAME") or "BAAI/bge-m3"
        self._batch_size = batch_size
        self._model: Any | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load_model()
        embeddings = model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in embedding] for embedding in embeddings]

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model

        self._model = _load_sentence_transformer(self._model_name)
        return self._model


@lru_cache(maxsize=None)
def _load_sentence_transformer(model_name: str) -> Any:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "BGE-M3 embedding generation requires sentence-transformers. "
            "Install llmPipeline requirements before enabling wiki page embedding jobs."
        ) from exc

    return SentenceTransformer(model_name)
