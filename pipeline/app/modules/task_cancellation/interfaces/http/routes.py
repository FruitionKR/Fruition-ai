import httpx

from fastapi import APIRouter, HTTPException
from app.modules.task_cancellation.infrastructure.backend_rollback import rollback_backend
from app.modules.agent_run.infrastructure.postgres_agent_run_repository import PostgresAgentRunRepository, register_cancelled_turn
from app.modules.task_cancellation.interfaces.http.schemas import CancelTaskRequest, TaskStatusResponse, DocumentCleanupResponse

from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal

router = APIRouter(prefix="/internal/ai/tasks", tags=["ai-task-cancellation"])


@router.post("/{run_id}/cancel", response_model=TaskStatusResponse)
def cancel_task(run_id: str, request: CancelTaskRequest):
    try:
        if request.command is not None:
            if any(str(request.command.get(key)) != value for key, value in (
                ("run_id", run_id), ("workspace_id", request.workspace_id), ("user_id", request.user_id),
            )):
                raise ValueError("Cancellation command identity mismatch.")
            if request.command.get("kind") == "agent":
                register_cancelled_turn(request.command)
                run = PostgresAgentRunRepository().cancel(request.workspace_id, request.user_id, run_id)
                return {"id": run.id, "status": run.status, "error_code": run.error_code}
            journal.register(request.command)
        journal.request_cancel(run_id, request.workspace_id, request.user_id)
        journal.rollback_pending()
        return journal.status(run_id, request.workspace_id, request.user_id)
    except KeyError as exc:
        raise HTTPException(404, "Task not found.") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{run_id}", response_model=TaskStatusResponse)
def task_status(run_id: str, workspace_id: str, user_id: str):
    try:
        journal.rollback_pending()
        return journal.status(run_id, workspace_id, user_id)
    except KeyError as exc:
        run = PostgresAgentRunRepository().get_for_user(workspace_id, user_id, run_id)
        if run is None:
            raise HTTPException(404, "Task not found.") from exc
        return {"id": run.id, "status": run.status, "error_code": run.error_code}


@router.post("/{run_id}/rollback-backend", status_code=204)
def rollback_backend_task(run_id: str, request: CancelTaskRequest):
    if request.command is None or request.command.get("kind") != "convert":
        state = task_status(run_id, request.workspace_id, request.user_id)
        if state["status"] != "cancelled":
            raise HTTPException(409, "AI rollback is incomplete.")
    try:
        rollback_backend(run_id, request.workspace_id, request.user_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            raise HTTPException(409, "Backend rollback conflicts with a later change.") from exc
        raise


@router.post("/documents/{document_id}/cancel", response_model=DocumentCleanupResponse)
def cancel_document_tasks(document_id: str, request: CancelTaskRequest):
    return {"cleanup_complete": journal.cancel_document_tasks(document_id, request.workspace_id, request.user_id)}
