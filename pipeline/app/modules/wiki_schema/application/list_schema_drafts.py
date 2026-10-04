from app.modules.wiki_schema.application.ports import WikiSchemaRepositoryPort
from app.modules.wiki_schema.domain.entities import WikiSchemaRecord


class ListSchemaDraftsUseCase:
    def __init__(self, repository: WikiSchemaRepositoryPort) -> None:
        self._repository = repository

    def execute(self, workspace_id: str, user_id: str) -> list[WikiSchemaRecord]:
        if not workspace_id.strip():
            raise ValueError("workspace_id is required.")
        if not user_id.strip():
            raise ValueError("user_id is required.")
        return self._repository.list_drafts(workspace_id.strip(), user_id.strip())
