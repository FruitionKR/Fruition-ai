from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class WikiPageEmbeddingTarget:
    page_id: str
    title: str
    summary: str
    markdown_uri: str
    updated_at: datetime


@dataclass(frozen=True)
class WikiPageEmbeddingInput:
    page_id: str
    representation: str
    representation_hash: str
    source_updated_at: datetime
