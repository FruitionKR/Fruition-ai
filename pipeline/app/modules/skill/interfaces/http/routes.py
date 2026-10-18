from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core.pipeline_control import (
    DOCUMENT_SYNC_DEADLINE_SECONDS,
    DeadlineExceededError,
    PipelineRunCancelledError,
    ReplayedResult,
    request_deadline,
)
from app.modules.model_usage.infrastructure.usage_ledger import usage_scope
from app.modules.model_usage.interfaces.http.dependencies import RequestId
from app.modules.skill.application.author_skill import AuthorSkillUseCase
from app.modules.markdown_edit.domain.entities import MarkdownEditTarget
from app.modules.skill.application.manage_skill import ManageSkillUseCase
from app.modules.skill.application.skill_test_run import require_test_runnable, skill_test_run_command
from app.modules.skill.application.propose_skill_draft import ProposeSkillDraftUseCase
from app.modules.skill.application.ports import SkillRepositoryPort
from app.modules.skill.domain.exceptions import (
    SKILL_REJECTION_CODES,
    ReferenceDocumentTooLargeError,
    SkillRequestRejectedError,
    SkillTestRunUnsupportedError,
)
from app.modules.skill.interfaces.http.dependencies import (
    get_author_skill_use_case,
    get_manage_skill_use_case,
    get_propose_skill_draft_use_case,
    get_skill_repository,
    get_skill_test_run_use_case,
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
    SkillTestRunRequest,
    SkillTestRunResponse,
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
        # document가 60초에 포기하므로 그 전에 끝내고, 넘기면 journal이 변경을 되돌린다(게시하지 않는다).
        with request_deadline(DOCUMENT_SYNC_DEADLINE_SECONDS):
            return journal.execute(task.model_dump(mode="json", exclude_none=True), execute)
    except SkillRequestRejectedError as exc:
        # journal 밖에서 HTTP로 바꿔야 journal이 거절 code를 error_code로 남긴다.
        raise HTTPException(400, {"code": exc.code, "message": str(exc)}) from exc
    except journal.TaskFailedError as exc:
        if exc.code in SKILL_REJECTION_CODES:
            raise HTTPException(400, {"code": exc.code, "message": "Skill task was already rejected."}) from exc
        raise HTTPException(409, {"code": exc.code, "message": "Skill task already failed."}) from exc
    except journal.TaskCommandMismatchError as exc:
        raise HTTPException(409, {"code": "task_command_mismatch", "message": str(exc)}) from exc
    except DeadlineExceededError as exc:
        raise HTTPException(504, {"code": exc.code, "message": str(exc)}) from exc
    except PipelineRunCancelledError as exc:
        raise HTTPException(409, {"code": "task_cancelled", "message": str(exc)}) from exc
    except journal.TaskAlreadyExecutingError as exc:
        raise HTTPException(409, {"code": "task_in_progress", "message": str(exc)}) from exc
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
    except SkillRequestRejectedError:
        raise
    except ValueError as exc:
        raise SkillRequestRejectedError("skill_request_invalid", str(exc)) from exc


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
    request_id: RequestId = None,
) -> SkillAuthoringResponse:
    use_case = get_propose_skill_draft_use_case(provider=payload.provider, model=payload.model)
    authorer = get_author_skill_use_case(provider=payload.provider, model=payload.model)
    scope = {"run_id": request_id, "workspace_id": payload.workspace_id,
             "user_id": payload.user_id, "kind": "skill_draft_preview"}
    try:
        with usage_scope(scope):
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


@router.post("/test-run", response_model=SkillTestRunResponse)
def run_skill_test(payload: SkillTestRunRequest, request_id: RequestId = None) -> SkillTestRunResponse:
    """저장하지 않는 Skill 테스트 실행. 사용량은 X-Request-Id run으로 기록하고 취소는 공통 작업 취소를 따른다."""
    if not request_id:
        raise HTTPException(400, {"code": "request_id_required", "message": "X-Request-Id is required."})
    definition = payload.definition
    try:
        require_test_runnable(tuple(definition.capabilities))
    except SkillTestRunUnsupportedError as exc:
        raise HTTPException(400, {"code": exc.code, "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
    # 실행 기록에는 신원만 남긴다. document가 취소 때 보내는 command(본문 대신 해시)와 형태가 달라도
    # 취소 경로가 같은 신원 command로 맞춰 등록하므로 해시가 어긋나지 않는다.
    command = skill_test_run_command(request_id, payload.workspace_id, payload.user_id)

    def execute() -> dict[str, object]:
        use_case = get_skill_test_run_use_case(provider=payload.provider, model=payload.model)
        target = payload.target
        result = use_case.execute(
            workspace_id=payload.workspace_id,
            user_id=payload.user_id,
            name=definition.name,
            description=definition.description,
            instructions_markdown=definition.instructions_markdown,
            capabilities=tuple(definition.capabilities),
            document_id=payload.document.document_id,
            markdown=payload.document.markdown,
            target=(
                MarkdownEditTarget(type=target.type, start_line=target.start_line, end_line=target.end_line)
                if target is not None
                else None
            ),
        )
        return SkillTestRunResponse.from_domain(result).model_dump(mode="json")

    try:
        with request_deadline(DOCUMENT_SYNC_DEADLINE_SECONDS):
            result = journal.execute(command, execute)
    except SkillRequestRejectedError as exc:
        raise HTTPException(400, {"code": exc.code, "message": str(exc)}) from exc
    except journal.TaskFailedError as exc:
        if exc.code in SKILL_REJECTION_CODES:
            raise HTTPException(400, {"code": exc.code, "message": "Skill test run was already rejected."}) from exc
        raise HTTPException(409, {"code": exc.code, "message": "Skill test run already failed."}) from exc
    except journal.TaskCommandMismatchError as exc:
        raise HTTPException(409, {"code": "task_command_mismatch", "message": str(exc)}) from exc
    except DeadlineExceededError as exc:
        raise HTTPException(504, {"code": exc.code, "message": str(exc)}) from exc
    except PipelineRunCancelledError as exc:
        raise HTTPException(409, {"code": "task_cancelled", "message": str(exc)}) from exc
    except journal.TaskAlreadyExecutingError as exc:
        raise HTTPException(409, {"code": "task_in_progress", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if isinstance(result, ReplayedResult):
        # 결과에 문서 본문이 있어 끝난 run은 결과를 지운다. 같은 run_id 재요청은 새 run_id로 다시 보내야 한다.
        raise HTTPException(409, {"code": "task_already_completed", "message": "Skill test run already completed."})
    return SkillTestRunResponse.model_validate(result)


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


@router.delete("/{skill_id}", status_code=204)
def delete_skill(
    skill_id: str,
    workspace_id: str,
    user_id: str,
    use_case: ManageSkillUseCase = Depends(get_manage_skill_use_case),
) -> Response:
    try:
        use_case.delete(workspace_id, user_id, skill_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Skill not found or not manageable.") from exc
    return Response(status_code=204)


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
