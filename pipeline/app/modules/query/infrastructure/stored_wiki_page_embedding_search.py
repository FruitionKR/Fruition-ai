import hashlib
import math
from functools import lru_cache

from app.modules.query.application.ports import EmbeddingSearchPort
from app.modules.query.domain.entities import SemanticQueryEmbedding
from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import BgeM3EmbeddingModel
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database


class StoredWikiPageEmbeddingSearch(EmbeddingSearchPort):
    def __init__(
        self,
        embedding_model: BgeM3EmbeddingModel | None = None,
        fallback_search: EmbeddingSearchPort | None = None,
    ) -> None:
        self._embedding_model = embedding_model or BgeM3EmbeddingModel()
        self._fallback_search = fallback_search
        self._cached_embed_query = lru_cache(maxsize=128)(self._embed_query)

    def embed_query(self, query: str) -> SemanticQueryEmbedding:
        return self._cached_embed_query(query)

    def _embed_query(self, query: str) -> SemanticQueryEmbedding:
        return SemanticQueryEmbedding(
            model_name=self._embedding_model.model_name,
            vector=self._embedding_model.embed([query])[0],
        )

    def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []

        document_hashes = [self._hash(document) for document in documents]
        stored_vectors = self._load_vectors_by_hash(document_hashes)
        scores: list[float | None] = [None for _ in documents]
        missing_indexes = []
        missing_documents = []

        query_vector = self.embed_query(query).vector
        for index, document_hash in enumerate(document_hashes):
            document_vector = stored_vectors.get(document_hash)
            if document_vector is None or len(document_vector) != len(query_vector):
                missing_indexes.append(index)
                missing_documents.append(documents[index])
                continue
            scores[index] = self._dot(query_vector, document_vector)

        if missing_documents:
            fallback_scores = (
                self._fallback_search.score(query, missing_documents)
                if self._fallback_search is not None
                else [
                    self._dot(query_vector, vector)
                    for vector in self._embedding_model.embed(missing_documents)
                ]
            )
            for index, fallback_score in zip(
                missing_indexes,
                fallback_scores,
                strict=True,
            ):
                scores[index] = fallback_score

        return [float(score or 0.0) for score in scores]

    def _load_vectors_by_hash(self, representation_hashes: list[str]) -> dict[str, list[float]]:
        unique_hashes = list(dict.fromkeys(representation_hashes))
        if not unique_hashes:
            return {}
        with database.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT ON (representation_hash)
                    representation_hash,
                    embedding_vector
                FROM (
                    SELECT representation_hash, embedding_vector, updated_at
                    FROM wiki_page_embeddings
                    WHERE embedding_model = %s
                      AND status = 'completed'
                      AND representation_hash = ANY(%s)
                    UNION ALL
                    SELECT representation_hash, embedding_vector, updated_at
                    FROM wiki_embedding_vectors
                    WHERE embedding_model = %s
                      AND status = 'completed'
                      AND representation_hash = ANY(%s)
                ) stored_embeddings
                ORDER BY representation_hash, updated_at DESC
                """,
                (
                    self._embedding_model.model_name,
                    unique_hashes,
                    self._embedding_model.model_name,
                    unique_hashes,
                ),
            ).fetchall()
        vectors = {}
        for row in rows:
            vector = self._parse_vector(row["embedding_vector"])
            if vector:
                vectors[row["representation_hash"]] = vector
        return vectors

    def _parse_vector(self, value) -> list[float]:
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned.startswith("[") and cleaned.endswith("]"):
                cleaned = cleaned[1:-1]
            parts = [part.strip() for part in cleaned.split(",") if part.strip()]
        else:
            parts = list(value or [])

        vector = []
        for part in parts:
            try:
                value = float(part)
            except (TypeError, ValueError):
                return []
            if not math.isfinite(value):
                return []
            vector.append(value)
        return vector

    def _hash(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _dot(self, left: list[float], right: list[float]) -> float:
        value = float(sum(a * b for a, b in zip(left, right, strict=True)))
        return max(0.0, min(1.0, value))
