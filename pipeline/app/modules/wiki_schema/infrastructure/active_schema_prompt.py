from app.modules.wiki_schema.application.build_active_schema_prompt import build_active_schema_prompt
from app.modules.wiki_schema.domain.entities import SchemaFeature
from app.modules.wiki_schema.infrastructure.postgres_wiki_schema_repository import PostgresWikiSchemaRepository


def get_active_schema_prompt(
    feature: SchemaFeature,
    workspace_id: str | None,
    user_id: str | None,
) -> str:
    if not workspace_id or not user_id:
        return ""
    try:
        return build_active_schema_prompt(
            repository=PostgresWikiSchemaRepository(),
            feature=feature,
            workspace_id=workspace_id,
            user_id=user_id,
        )
    except Exception:
        return ""
