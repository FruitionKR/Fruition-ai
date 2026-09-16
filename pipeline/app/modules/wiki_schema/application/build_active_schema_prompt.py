from app.modules.wiki_schema.application.build_workspace_schema_prompt import build_workspace_schema_prompt
from app.modules.wiki_schema.application.ports import WikiSchemaRepositoryPort
from app.modules.wiki_schema.domain.entities import SchemaFeature


def build_active_schema_prompt(
    repository: WikiSchemaRepositoryPort,
    feature: SchemaFeature,
    workspace_id: str,
    user_id: str,
) -> str:
    active_schema = repository.get_active(workspace_id, user_id)
    if active_schema is None:
        return ""
    return build_workspace_schema_prompt(active_schema.fragments, feature)
