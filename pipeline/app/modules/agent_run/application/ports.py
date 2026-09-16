from contextlib import AbstractContextManager
from typing import Any, Protocol

from app.modules.agent_run.domain.entities import (
    AgentJob,
    AgentRun,
    AgentRunContext,
    StartAgentRunArtifact,
    ContentArtifactReference,
    StartAgentRunRequest,
)
from app.modules.agent_run.domain.plan import AgentPlan


class ToolGatewayError(RuntimeError):
    def __init__(self, status_code: int | None, retryable: bool) -> None:
        super().__init__(f"Agent tool gateway failed with status {status_code}.")
        self.status_code = status_code
        self.retryable = retryable


class AgentRunRepositoryPort(Protocol):
    def create_with_planning_job(
        self,
        run: AgentRun,
        job_id: str,
        artifact: StartAgentRunArtifact | None = None,
    ) -> AgentRun:
        ...

    def get_for_user(self, workspace_id: str, user_id: str, run_id: str) -> AgentRun | None:
        ...


class AgentRunStarterPort(Protocol):
    def start(self, request: StartAgentRunRequest) -> tuple[str, str]:
        ...


class AgentApprovalRepositoryPort(Protocol):
    def get_current_plan_for_user(
        self, workspace_id: str, user_id: str, run_id: str
    ) -> tuple[AgentRun, AgentPlan] | None:
        ...

    def approve_and_enqueue(
        self,
        run: AgentRun,
        plan: AgentPlan,
        approval_id: str,
        job_id: str,
    ) -> AgentRun:
        ...


class AgentRunManagementRepositoryPort(AgentApprovalRepositoryPort, Protocol):
    def get_for_user(self, workspace_id: str, user_id: str, run_id: str) -> AgentRun | None:
        ...

    def get_markdown_turn_status(
        self, workspace_id: str, user_id: str, run_id: str
    ) -> dict[str, object] | None:
        ...

    def reject(self, workspace_id: str, user_id: str, run_id: str, approval_id: str) -> AgentRun:
        ...

    def cancel(self, workspace_id: str, user_id: str, run_id: str) -> AgentRun:
        ...

    def revise(
        self,
        workspace_id: str,
        user_id: str,
        run_id: str,
        instruction: str,
        job_id: str,
    ) -> AgentRun:
        ...


class AgentToolAuthorizationRepositoryPort(Protocol):
    def authorize_tool_read(self, workspace_id: str, user_id: str, run_id: str) -> bool:
        ...

    def authorize_tool_execute(
        self,
        *,
        run_id: str,
        workspace_id: str,
        user_id: str,
        plan_id: str,
        plan_version: int,
        operation_hash: str,
        operation_id: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> bool:
        ...

    def register_artifact(
        self,
        *,
        run_id: str,
        workspace_id: str,
        user_id: str,
        artifact_id: str,
        content_hash: str,
        purpose: str,
        document_id: str | None,
        base_version: int | None,
        target: dict[str, object] | None,
        markdown: str,
    ) -> dict[str, object]:
        ...

    def list_artifacts(self, workspace_id: str, user_id: str, run_id: str) -> list[dict[str, object]]:
        ...

    def resolve_artifact(
        self,
        *,
        run_id: str,
        workspace_id: str,
        user_id: str,
        artifact_id: str,
        content_hash: str,
        purpose: str,
        document_id: str | None,
        base_version: int | None,
        target: dict[str, object] | None,
    ) -> dict[str, object] | None:
        ...


class AgentPlanRepositoryPort(Protocol):
    def register_artifact(
        self, *, run_id: str, workspace_id: str, user_id: str, artifact_id: str,
        content_hash: str, purpose: str, document_id: str | None,
        base_version: int | None, target: dict[str, object] | None, markdown: str,
    ) -> dict[str, object]:
        ...

    def save_plan(self, run_id: str, plan: AgentPlan) -> None:
        ...


class AgentPlanGeneratorPort(Protocol):
    def generate(
        self,
        *,
        run_id: str,
        plan_id: str,
        version: int,
        instruction: str,
        routing_action: str,
        hierarchy: list[dict[str, object]],
        skill_instructions: str | None,
        allowed_tools: tuple[str, ...] | None,
        content_artifacts: tuple[ContentArtifactReference, ...] = (),
    ) -> AgentPlan:
        ...


class AgentToolGatewayPort(Protocol):
    def read(
        self,
        tool_name: str,
        *,
        run_id: str,
        workspace_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        ...

    def execute(
        self,
        tool_name: str,
        *,
        run_id: str,
        workspace_id: str,
        user_id: str,
        plan_id: str,
        plan_version: int,
        operation_hash: str,
        operation_id: str,
        idempotency_key: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        ...


class AgentJobRepositoryPort(Protocol):
    def execution_lock(self, run_id: str) -> AbstractContextManager[None]:
        ...

    def get_undo_record(self, run_id: str, operation_id: str) -> dict[str, Any] | None:
        ...

    def prepare_undo(self, run_id: str, plan_id: str, operation_id: str,
                     tool_name: str, arguments: dict[str, Any], before: dict[str, Any]) -> bool:
        ...

    def load_undo_records(self, run_id: str) -> list[dict[str, Any]]:
        ...

    def save_undo_request(self, record_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
        ...

    def complete_undo(self, record_id: str, response: dict[str, Any]) -> None:
        ...

    def parent_rollback_error(self, run_id: str) -> str | None:
        ...

    def finish_rollback(self, run_id: str, error_code: str | None = None) -> None:
        ...

    def heartbeat(self, job: AgentJob) -> bool:
        ...

    def complete(self, job: AgentJob) -> None:
        ...

    def fail(self, job: AgentJob, error_code: str) -> None:
        ...

    def load_context(self, run_id: str) -> AgentRunContext:
        ...

    def mark_run_status(self, run_id: str, expected: tuple[str, ...], status: str) -> bool:
        ...

    def reserve_tool_call(self, run_id: str) -> bool:
        ...

    def remaining_tool_calls(self, run_id: str) -> int:
        ...

    def request_clarification(self, run_id: str, error_code: str) -> bool:
        ...

    def next_plan_version(self, run_id: str) -> int:
        ...

    def load_current_plan(self, run_id: str) -> AgentPlan:
        ...

    def mark_operation(
        self,
        operation_id: str,
        from_statuses: tuple[str, ...],
        status: str,
        error_code: str | None = None,
    ) -> bool:
        ...

    def save_tool_execution(
        self,
        *,
        run_id: str,
        plan_id: str,
        operation_id: str,
        tool_name: str,
        idempotency_key: str,
        attempt: int,
        status: str,
        response_metadata: dict[str, object],
        error_code: str | None,
    ) -> None:
        ...

    def load_operation_results(self, run_id: str, plan_id: str) -> dict[str, dict[str, object]]:
        ...

    def finish_run_from_operations(self, run_id: str) -> None:
        ...
