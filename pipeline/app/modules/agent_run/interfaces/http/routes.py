from uuid import uuid4

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.modules.agent_run.application.approve_agent_plan import ApproveAgentPlanUseCase
from app.modules.agent_run.application.mcp_approval import ApproveMcpAgentPlanUseCase, McpApprovalError
from app.modules.agent_run.application.mcp_proposal import (
    CreateMcpProposalUseCase,
    McpProposalError,
)
from app.modules.agent_run.application.ports import (
    AgentRunManagementRepositoryPort,
    AgentToolAuthorizationRepositoryPort,
)
from app.modules.agent_run.domain.plan import AgentPlanSelectionInvalid
from app.core.pipeline_control import ScopePurgedError
from app.modules.agent_run.interfaces.http.dependencies import (
    get_agent_run_repository,
    get_approve_agent_plan_use_case,
    get_approve_mcp_agent_plan_use_case,
    get_create_mcp_proposal_use_case,
)
from app.modules.agent_run.interfaces.http.schemas import (
    AgentArtifactListRequest,
    AgentArtifactRegisterRequest,
    AgentArtifactResolveRequest,
    AgentArtifactResolveResponse,
    AgentArtifactResponse,
    AgentRunActorRequest,
    AgentRunEditPreviewsResponse,
    AgentRunResponse,
    AgentToolExecuteAuthorizationRequest,
    AgentToolReadAuthorizationRequest,
    ApproveAgentPlanRequest,
    MarkdownAgentRunStatusResponse,
    McpAgentProposalRequest,
    McpAgentProposalResponse,
    McpAgentApprovalRequest,
    McpOwnedRunsResponse,
    ReviseAgentPlanRequest,
)

router = APIRouter(prefix="/agent/runs", tags=["agent-runs"])
proposal_router = APIRouter(prefix="/agent/runs", tags=["mcp-agent-proposals"])
approval_router = APIRouter(prefix="/agent/runs", tags=["agent-runs"])
internal_router = APIRouter(prefix="/internal/agent/runs", tags=["internal-agent-runs"])
_MCP_PROPOSAL_MAX_REQUEST_BYTES = 4 * 1024 * 1024


def _mcp_error(error: McpProposalError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
                "handle": None,
                "state": None,
                "operation_error": error.operation_error,
            }
        },
    )


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object key")
        value[key] = item
    return value


@proposal_router.post("/mcp-proposals", response_model=McpAgentProposalResponse)
async def create_mcp_proposal(
    request: Request,
    use_case: CreateMcpProposalUseCase = Depends(get_create_mcp_proposal_use_case),
):
    try:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MCP_PROPOSAL_MAX_REQUEST_BYTES:
                raise McpProposalError(413, "REQUEST_TOO_LARGE", "MCP proposal request is too large.")
            body.extend(chunk)
        try:
            value = json.loads(
                body,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite number")),
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise McpProposalError(400, "INVALID_INPUT", "MCP proposal request is invalid JSON.") from None
        try:
            payload = McpAgentProposalRequest.model_validate(value)
        except ValidationError:
            raise McpProposalError(400, "INVALID_INPUT", "MCP proposal request is invalid.") from None
        result = await run_in_threadpool(use_case.execute, payload.to_application())
        return McpAgentProposalResponse(
            run_id=result.run_id,
            plan_id=result.plan_id,
            plan_version=result.plan_version,
            operation_hash=result.operation_hash,
            status=result.status,
        )
    except McpProposalError as exc:
        return _mcp_error(exc)
    except ScopePurgedError:
        return _mcp_error(McpProposalError(403, "PERMISSION_REVOKED", "Proposal scope was purged."))
    except ValueError:
        return _mcp_error(McpProposalError(400, "INVALID_INPUT", "MCP proposal validation failed."))
    except Exception:
        return _mcp_error(
            McpProposalError(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "MCP proposal creation is temporarily unavailable.",
                retryable=True,
            )
        )


@proposal_router.get("/mcp-owned", response_model=McpOwnedRunsResponse)
def list_owned_mcp_runs(
    workspace_id: str,
    user_id: str,
    grant_id: str,
    cursor: str | None = None,
    limit: int = 20,
    repository: AgentRunManagementRepositoryPort = Depends(get_agent_run_repository),
) -> dict[str, object]:
    if not 1 <= limit <= 20:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 20.")
    try:
        return repository.list_owned_mcp_runs(workspace_id, user_id, grant_id, cursor, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid MCP run cursor or identity.") from exc


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
        approval_id=payload.approval_id,
        attempt_id=payload.attempt_id,
        grant_id=payload.grant_id,
        client_id=payload.client_id,
        policy_generation=payload.policy_generation,
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


@router.get("/{run_id}/edit-previews", response_model=AgentRunEditPreviewsResponse)
def get_agent_run_edit_previews(
    run_id: str,
    workspace_id: str,
    user_id: str,
    repository: AgentRunManagementRepositoryPort = Depends(get_agent_run_repository),
) -> dict[str, object]:
    previews = repository.get_edit_previews(workspace_id, user_id, run_id)
    if previews is None:
        raise HTTPException(status_code=404, detail="AgentRun not found.")
    return previews


@approval_router.post("/{run_id}/approve", response_model=AgentRunResponse)
def approve_agent_run(
    run_id: str,
    payload: McpAgentApprovalRequest | ApproveAgentPlanRequest,
    use_case: ApproveAgentPlanUseCase = Depends(get_approve_agent_plan_use_case),
    mcp_use_case: ApproveMcpAgentPlanUseCase = Depends(get_approve_mcp_agent_plan_use_case),
) -> AgentRunResponse:
    try:
        if isinstance(payload, McpAgentApprovalRequest):
            run = mcp_use_case.execute(payload.to_application(run_id))
        else:
            run = use_case.execute(
                workspace_id=payload.workspace_id,
                user_id=payload.user_id,
                run_id=run_id,
                plan_version=payload.plan_version,
                operation_hash=payload.operation_hash,
                operation_ids=payload.operation_ids,
                approved_by=payload.approved_by,
            )
    except McpApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
    except ScopePurgedError:
        raise HTTPException(status_code=409, detail={"code": "PURGED", "message": "Agent run scope was purged."}) from None
    except AgentPlanSelectionInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
