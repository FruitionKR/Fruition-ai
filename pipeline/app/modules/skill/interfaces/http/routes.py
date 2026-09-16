from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.modules.skill.application.author_skill import AuthorSkillUseCase
from app.modules.skill.application.manage_skill import ManageSkillUseCase
from app.modules.skill.application.propose_skill_draft import ProposeSkillDraftUseCase
from app.modules.skill.application.ports import SkillRepositoryPort
from app.modules.skill.domain.exceptions import ReferenceDocumentTooLargeError
from app.modules.skill.interfaces.http.dependencies import (
    get_author_skill_use_case,
    get_manage_skill_use_case,
    get_propose_skill_draft_use_case,
    get_skill_repository,
)
from app.modules.skill.interfaces.http.schemas import (
    SkillTaskRequest,
    PublishAuthoredSkillRequest,
    SkillAuthoringRequest,
    SkillAuthoringResponse,
    SkillActorRequest,
    SkillDefinitionRequest,
    SkillDraftProposalRequest,
    SkillPreviewResponse,
    SkillResponse,
    UpdateSkillRequest,
)


router = APIRouter(prefix="/skills", tags=["skills"])
agent_router = APIRouter(prefix="/skills", tags=["skills"])


@router.post("/tasks")
def execute_skill_task(task: SkillTaskRequest):
    from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
    if any(task.payload.get(key) != getattr(task, key) for key in ("workspace_id", "user_id")):
        raise HTTPException(409, "Skill task actor mismatch.")
    schema = {"skill_author": SkillAuthoringRequest, "skill_publish": PublishAuthoredSkillRequest,
              "skill_update": UpdateSkillRequest}[task.kind]
    try:
        payload = schema.model_validate(task.payload)
    except ValidationError as exc:
        raise RequestValidationError([
            {**error, "loc": ("body", "payload", *error["loc"])} for error in exc.errors()
        ]) from exc
    def execute():
        if task.kind == "skill_author":
            result = author_skill(payload)
        elif task.kind == "skill_publish":
            result = publish_authored_skill(payload)
        else:
            if not task.skill_id:
                raise HTTPException(400, "skill_id is required.")
            result = update_skill(task.skill_id, payload)
        if isinstance(result, JSONResponse):
            import json
            raise HTTPException(result.status_code, json.loads(result.body))
        return {**result.model_dump(mode="json"), "run_id": task.run_id}
    try:
        return journal.execute(task.model_dump(mode="json", exclude_none=True), execute)
    except HTTPException as exc:
        if exc.status_code == 413:
            return JSONResponse(status_code=413, content=exc.detail)
        raise


def author_skill(
    payload: SkillAuthoringRequest,
) -> SkillAuthoringResponse | JSONResponse:
    use_case = get_author_skill_use_case(provider=payload.provider, model=payload.model)
    try:
        result = use_case.execute(
            workspace_id=payload.workspace_id,
            user_id=payload.user_id,
            scope_type=payload.scope_type,
            name=payload.name,
            description=payload.description,
            instruction=payload.instruction,
            authoring_mode=payload.authoring_mode,
            reference_document_ids=tuple(payload.reference_document_ids),
            allow_clarification=False,
        )
        return SkillAuthoringResponse.from_domain(result)
    except ReferenceDocumentTooLargeError as exc:
        return JSONResponse(
            status_code=413,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def publish_authored_skill(
    payload: PublishAuthoredSkillRequest,
) -> SkillAuthoringResponse:
    use_case = get_author_skill_use_case(provider=payload.provider, model=payload.model)
    try:
        result = use_case.publish(
            workspace_id=payload.workspace_id,
            user_id=payload.user_id,
            scope_type=payload.scope_type,
            name=payload.name,
            description=payload.description,
            instructions_markdown=payload.instructions_markdown,
            expected_capabilities=tuple(payload.capabilities),
            expected_allowed_tools=tuple(payload.allowed_tools),
        )
        return SkillAuthoringResponse.from_domain(result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@agent_router.post("/draft-from-runs/preview", response_model=SkillAuthoringResponse)
def propose_skill_draft(
    payload: SkillDraftProposalRequest,
) -> SkillAuthoringResponse:
    use_case = get_propose_skill_draft_use_case(provider=payload.provider, model=payload.model)
    authorer = get_author_skill_use_case(provider=payload.provider, model=payload.model)
    try:
        proposal = use_case.execute(
            source_runs=tuple(source.to_domain() for source in payload.source_runs),
            user_directives=tuple(payload.user_directives),
            excluded_literals=tuple(payload.excluded_literals),
        )
        reviewed = authorer.review_draft(
            workspace_id=payload.workspace_id,
            user_id=payload.user_id,
            scope_type=payload.scope_type,
            draft=proposal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SkillAuthoringResponse.from_domain(reviewed)


@router.post("/preview", response_model=SkillPreviewResponse)
def preview_skill(
    payload: SkillDefinitionRequest,
    use_case: ManageSkillUseCase = Depends(get_manage_skill_use_case),
) -> SkillPreviewResponse:
    try:
        version = use_case.preview(
            user_id=payload.user_id,
            name=payload.name,
            description=payload.description,
            instructions_markdown=payload.instructions_markdown,
            capabilities=tuple(payload.capabilities),
            allowed_tools=tuple(payload.allowed_tools),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SkillPreviewResponse.from_domain(version)


@router.get("", response_model=list[SkillResponse])
def list_skills(
    workspace_id: str,
    user_id: str,
    repository: SkillRepositoryPort = Depends(get_skill_repository),
) -> list[SkillResponse]:
    return [SkillResponse.from_domain(skill) for skill in repository.list_accessible(workspace_id, user_id)]


@router.get("/{skill_id}", response_model=SkillResponse)
def get_skill(
    skill_id: str,
    workspace_id: str,
    user_id: str,
    repository: SkillRepositoryPort = Depends(get_skill_repository),
) -> SkillResponse:
    skill = repository.get_accessible(workspace_id, user_id, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found.")
    return SkillResponse.from_domain(skill)


def update_skill(
    skill_id: str,
    payload: UpdateSkillRequest,
) -> SkillAuthoringResponse:
    use_case = get_author_skill_use_case(provider=payload.provider, model=payload.model)
    try:
        result = use_case.update(
            workspace_id=payload.workspace_id,
            user_id=payload.user_id,
            skill_id=skill_id,
            name=payload.name,
            description=payload.description,
            instructions_markdown=payload.instructions_markdown,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SkillAuthoringResponse.from_domain(result)


@router.post("/{skill_id}/enable", response_model=SkillResponse)
def enable_skill(
    skill_id: str,
    payload: SkillActorRequest,
    use_case: ManageSkillUseCase = Depends(get_manage_skill_use_case),
) -> SkillResponse:
    return _set_enabled(use_case, payload, skill_id, True)


@router.post("/{skill_id}/disable", response_model=SkillResponse)
def disable_skill(
    skill_id: str,
    payload: SkillActorRequest,
    use_case: ManageSkillUseCase = Depends(get_manage_skill_use_case),
) -> SkillResponse:
    return _set_enabled(use_case, payload, skill_id, False)


def _set_enabled(
    use_case: ManageSkillUseCase,
    payload: SkillActorRequest,
    skill_id: str,
    enabled: bool,
) -> SkillResponse:
    try:
        skill = use_case.set_enabled(payload.workspace_id, payload.user_id, skill_id, enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SkillResponse.from_domain(skill)
