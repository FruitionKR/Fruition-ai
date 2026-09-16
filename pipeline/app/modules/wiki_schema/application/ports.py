from typing import Protocol

from app.modules.wiki_schema.domain.entities import WikiSchemaRecord, SchemaOrganizerCandidate


class SchemaOrganizerPort(Protocol):
    def organize(self, raw_markdown: str) -> SchemaOrganizerCandidate:
        ...


class WikiSchemaRepositoryPort(Protocol):
    def save(self, record: WikiSchemaRecord) -> WikiSchemaRecord:
        ...

    def get(self, schema_id: str) -> WikiSchemaRecord | None:
        ...

    def activate(self, schema_id: str) -> WikiSchemaRecord:
        ...

    def get_active(self, workspace_id: str, user_id: str) -> WikiSchemaRecord | None:
        ...
