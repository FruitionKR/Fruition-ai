from app.modules.wiki_schema.application.ports import WikiSchemaRepositoryPort
from app.modules.wiki_schema.domain.entities import WikiSchemaRecord


class GetActiveSchemaUseCase:
    def __init__(self, repository: WikiSchemaRepositoryPort) -> None:
        self._repository = repository

    def execute(self, workspace_id: str, user_id: str) -> WikiSchemaRecord | None:
        if not workspace_id.strip():
            raise ValueError("workspace_id is required.")
        if not user_id.strip():
            raise ValueError("user_id is required.")
        return self._repository.get_active(workspace_id.strip(), user_id.strip())
