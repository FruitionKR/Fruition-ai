from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.modules.agent_run.application.approve_agent_plan import ApproveAgentPlanUseCase
from app.modules.agent_run.application.ports import (
    AgentRunManagementRepositoryPort,
    AgentToolAuthorizationRepositoryPort,
)
from app.modules.agent_run.interfaces.http.dependencies import (
    get_agent_run_repository,
    get_approve_agent_plan_use_case,
)
from app.modules.agent_run.interfaces.http.schemas import (
    AgentArtifactListRequest,
    AgentArtifactRegisterRequest,
    AgentArtifactResolveRequest,
    AgentArtifactResolveResponse,
    AgentArtifactResponse,
    AgentRunActorRequest,
    AgentRunResponse,
    AgentToolExecuteAuthorizationRequest,
    AgentToolReadAuthorizationRequest,
    ApproveAgentPlanRequest,
    MarkdownAgentRunStatusResponse,
    ReviseAgentPlanRequest,
)

router = APIRouter(prefix="/agent/runs", tags=["agent-runs"])
internal_router = APIRouter(prefix="/internal/agent/runs", tags=["internal-agent-runs"])


@internal_router.post("/tool-authorizations/read", status_code=status.HTTP_204_NO_CONTENT)
def authorize_agent_tool_read(
    payload: AgentToolReadAuthorizationRequest,
    repository: AgentToolAuthorizationRepositoryPort = Depends(get_agent_run_repository),
) -> Response:
    if not repository.authorize_tool_read(payload.workspace_id, payload.user_id, payload.run_id):
        raise HTTPException(status_code=404, detail="AgentRun not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@internal_router.post("/artifacts/register", response_model=AgentArtifactResponse)
def register_agent_artifact(
    payload: AgentArtifactRegisterRequest,
    repository: AgentToolAuthorizationRepositoryPort = Depends(get_agent_run_repository),
) -> dict[str, object]:
    try:
        return repository.register_artifact(
            run_id=payload.run_id,
            workspace_id=payload.workspace_id,
            user_id=payload.user_id,
            artifact_id=payload.artifact_id,
            content_hash=payload.content_hash,
            purpose=payload.purpose,
            document_id=payload.document_id,
            base_version=payload.base_version,
            target=payload.target,
            markdown=payload.markdown,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="AgentRun or artifact not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@internal_router.post("/artifacts/list", response_model=list[AgentArtifactResponse])
def list_agent_artifacts(
    payload: AgentArtifactListRequest,
    repository: AgentToolAuthorizationRepositoryPort = Depends(get_agent_run_repository),
) -> list[dict[str, object]]:
    return repository.list_artifacts(payload.workspace_id, payload.user_id, payload.run_id)


@internal_router.post("/artifacts/resolve", response_model=AgentArtifactResolveResponse)
def resolve_agent_artifact(
    payload: AgentArtifactResolveRequest,
    repository: AgentToolAuthorizationRepositoryPort = Depends(get_agent_run_repository),
) -> dict[str, object]:
    try:
        artifact = repository.resolve_artifact(
            run_id=payload.run_id,
            workspace_id=payload.workspace_id,
            user_id=payload.user_id,
            artifact_id=payload.artifact_id,
            content_hash=payload.content_hash,
            purpose=payload.purpose,
            document_id=payload.document_id,
            base_version=payload.base_version,
            target=payload.target,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if artifact is None:
        raise HTTPException(status_code=404, detail="Agent artifact not found.")
    return artifact


@internal_router.post("/tool-authorizations/execute", status_code=status.HTTP_204_NO_CONTENT)
def authorize_agent_tool_execute(
    payload: AgentToolExecuteAuthorizationRequest,
    repository: AgentToolAuthorizationRepositoryPort = Depends(get_agent_run_repository),
) -> Response:
    if not repository.authorize_tool_execute(
        run_id=payload.run_id,
        workspace_id=payload.workspace_id,
        user_id=payload.user_id,
        plan_id=payload.plan_id,
        plan_version=payload.plan_version,
        operation_hash=payload.operation_hash,
        operation_id=payload.operation_id,
        tool_name=payload.tool_name,
        arguments=payload.arguments,
    ):
        raise HTTPException(status_code=409, detail="Approved Agent operation does not match.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@internal_router.get("/{run_id}", response_model=MarkdownAgentRunStatusResponse)
def get_markdown_agent_run(
    run_id: str,
    workspace_id: str,
    user_id: str,
    repository: AgentRunManagementRepositoryPort = Depends(get_agent_run_repository),
) -> MarkdownAgentRunStatusResponse:
    run = repository.get_markdown_turn_status(workspace_id, user_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="AgentRun not found.")
    return MarkdownAgentRunStatusResponse.model_validate(run)


@router.get("/{run_id}", response_model=AgentRunResponse)
def get_agent_run(
    run_id: str,
    workspace_id: str,
    user_id: str,
    repository: AgentRunManagementRepositoryPort = Depends(get_agent_run_repository),
) -> AgentRunResponse:
    current = repository.get_current_plan_for_user(workspace_id, user_id, run_id)
    if current:
        return AgentRunResponse.from_domain(*current)
    run = repository.get_for_user(workspace_id, user_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="AgentRun not found.")
    return AgentRunResponse.from_domain(run)


@router.post("/{run_id}/approve", response_model=AgentRunResponse)
def approve_agent_run(
    run_id: str,
    payload: ApproveAgentPlanRequest,
    use_case: ApproveAgentPlanUseCase = Depends(get_approve_agent_plan_use_case),
) -> AgentRunResponse:
    try:
        run = use_case.execute(
            workspace_id=payload.workspace_id,
            user_id=payload.user_id,
            run_id=run_id,
            plan_version=payload.plan_version,
            operation_hash=payload.operation_hash,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AgentRunResponse.from_domain(run)


@router.post("/{run_id}/reject", response_model=AgentRunResponse)
def reject_agent_run(
    run_id: str,
    payload: AgentRunActorRequest,
    repository: AgentRunManagementRepositoryPort = Depends(get_agent_run_repository),
) -> AgentRunResponse:
    try:
        run = repository.reject(payload.workspace_id, payload.user_id, run_id, str(uuid4()))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AgentRunResponse.from_domain(run)


@router.post("/{run_id}/cancel", response_model=AgentRunResponse)
def cancel_agent_run(
    run_id: str,
    payload: AgentRunActorRequest,
    repository: AgentRunManagementRepositoryPort = Depends(get_agent_run_repository),
) -> AgentRunResponse:
    try:
        run = repository.cancel(payload.workspace_id, payload.user_id, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AgentRunResponse.from_domain(run)


@router.post("/{run_id}/revise", response_model=AgentRunResponse)
def revise_agent_run(
    run_id: str,
    payload: ReviseAgentPlanRequest,
    repository: AgentRunManagementRepositoryPort = Depends(get_agent_run_repository),
) -> AgentRunResponse:
    try:
        run = repository.revise(
            payload.workspace_id,
            payload.user_id,
            run_id,
            payload.instruction,
            str(uuid4()),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AgentRunResponse.from_domain(run)
