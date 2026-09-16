from fastapi import APIRouter, Depends, HTTPException

from app.modules.wiki_schema.application.activate_schema import ActivateSchemaUseCase
from app.modules.wiki_schema.application.build_schema_preview import build_schema_preview
from app.modules.wiki_schema.application.create_schema_draft import CreateSchemaDraftUseCase
from app.modules.wiki_schema.application.get_active_schema import GetActiveSchemaUseCase
from app.modules.wiki_schema.application.organize_schema import OrganizeSchemaUseCase
from app.modules.wiki_schema.interfaces.http.dependencies import (
    get_activate_schema_use_case,
    get_active_schema_use_case,
    get_create_schema_draft_use_case,
    get_organize_schema_use_case,
)
from app.modules.wiki_schema.interfaces.http.schemas import (
    CreateWikiSchemaDraftRequest,
    CreateWikiSchemaDraftResponse,
    WikiSchemaPreviewRequest,
    WikiSchemaPreviewResponse,
    WikiSchemaResponse,
)


router = APIRouter(prefix="/wiki-schema", tags=["wiki-schema"])


@router.post("/preview", response_model=WikiSchemaPreviewResponse)
def preview_wiki_schema(
    payload: WikiSchemaPreviewRequest,
    use_case: OrganizeSchemaUseCase = Depends(get_organize_schema_use_case),
) -> WikiSchemaPreviewResponse:
    try:
        result = use_case.execute(payload.raw_markdown)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return WikiSchemaPreviewResponse.from_domain(
        result=result,
        preview_markdown=build_schema_preview(result),
    )


@router.post("/drafts", response_model=CreateWikiSchemaDraftResponse)
def create_wiki_schema_draft(
    payload: CreateWikiSchemaDraftRequest,
    use_case: CreateSchemaDraftUseCase = Depends(get_create_schema_draft_use_case),
) -> CreateWikiSchemaDraftResponse:
    try:
        record = use_case.execute(
            raw_markdown=payload.raw_markdown,
            workspace_id=payload.workspace_id,
            user_id=payload.user_id,
            name=payload.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return CreateWikiSchemaDraftResponse(wiki_schema=WikiSchemaResponse.from_domain(record))


@router.post("/{schema_id}/activate", response_model=WikiSchemaResponse)
def activate_wiki_schema(
    schema_id: str,
    use_case: ActivateSchemaUseCase = Depends(get_activate_schema_use_case),
) -> WikiSchemaResponse:
    try:
        record = use_case.execute(schema_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return WikiSchemaResponse.from_domain(record)


@router.get("/active", response_model=WikiSchemaResponse | None)
def get_active_wiki_schema(
    workspace_id: str,
    user_id: str,
    use_case: GetActiveSchemaUseCase = Depends(get_active_schema_use_case),
) -> WikiSchemaResponse | None:
    try:
        record = use_case.execute(workspace_id, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WikiSchemaResponse.from_domain(record) if record else None
