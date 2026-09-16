from app.modules.wiki_schema.application.ports import WikiSchemaRepositoryPort
from app.modules.wiki_schema.domain.entities import WikiSchemaRecord


class ActivateSchemaUseCase:
    def __init__(self, repository: WikiSchemaRepositoryPort) -> None:
        self._repository = repository

    def execute(self, schema_id: str) -> WikiSchemaRecord:
        if not schema_id.strip():
            raise ValueError("schema_id is required.")
        return self._repository.activate(schema_id)
