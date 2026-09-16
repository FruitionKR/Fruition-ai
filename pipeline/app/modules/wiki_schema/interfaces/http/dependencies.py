from functools import lru_cache

from fastapi import HTTPException

from app.modules.wiki_schema.application.activate_schema import ActivateSchemaUseCase
from app.modules.wiki_schema.application.create_schema_draft import CreateSchemaDraftUseCase
from app.modules.wiki_schema.application.get_active_schema import GetActiveSchemaUseCase
from app.modules.wiki_schema.application.organize_schema import OrganizeSchemaUseCase
from app.modules.wiki_schema.infrastructure.chat_completions_schema_organizer import build_schema_organizer
from app.modules.wiki_schema.infrastructure.postgres_wiki_schema_repository import PostgresWikiSchemaRepository


@lru_cache(maxsize=1)
def get_organize_schema_use_case() -> OrganizeSchemaUseCase:
    try:
        organizer = build_schema_organizer()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return OrganizeSchemaUseCase(organizer=organizer)


@lru_cache(maxsize=1)
def get_wiki_schema_repository() -> PostgresWikiSchemaRepository:
    return PostgresWikiSchemaRepository()


def get_create_schema_draft_use_case() -> CreateSchemaDraftUseCase:
    try:
        organizer = build_schema_organizer()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return CreateSchemaDraftUseCase(
        organizer=organizer,
        repository=get_wiki_schema_repository(),
    )


def get_activate_schema_use_case() -> ActivateSchemaUseCase:
    return ActivateSchemaUseCase(repository=get_wiki_schema_repository())


def get_active_schema_use_case() -> GetActiveSchemaUseCase:
    return GetActiveSchemaUseCase(repository=get_wiki_schema_repository())
