from typing import Protocol
from datetime import datetime

from app.modules.wiki_embedding.domain.entities import WikiPageEmbeddingTarget


class WikiPageEmbeddingRepositoryPort(Protocol):
    def list_active_pages_by_ids(self, page_ids: list[str]) -> list[WikiPageEmbeddingTarget]:
        ...

    def existing_hashes(self, page_ids: list[str], embedding_model: str) -> dict[str, str]:
        ...

    def upsert_embedding(
        self,
        page_id: str,
        embedding_model: str,
        representation_hash: str,
        embedding_vector: list[float],
        source_updated_at: datetime,
    ) -> None:
        ...

    def mark_failed(self, page_id: str, embedding_model: str, representation_hash: str,
                    error: str, source_updated_at: datetime) -> None:
        ...


class EmbeddingModelPort(Protocol):
    @property
    def model_name(self) -> str:
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class MarkdownReaderPort(Protocol):
    def read_markdown(self, markdown_uri: str) -> str:
        ...
