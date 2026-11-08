from dataclasses import replace
from uuid import uuid4

from app.modules.agent_run.application.ports import (
    AgentApprovalRepositoryPort,
    AgentToolGatewayPort,
    ToolGatewayError,
)
from app.modules.agent_run.domain.entities import AgentRun, McpApprovalCommand
from app.modules.agent_run.domain.plan import (
    AgentPlanSelectionInvalid,
    selected_operation_ids,
    validate_operation_ids_format,
)


_HALT_REASONS = {
    "GRANT_INACTIVE",
    "CLIENT_DISABLED",
    "POLICY_DISABLED",
    "POLICY_GENERATION_MISMATCH",
    "PURGED",
}


class McpApprovalError(ValueError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class ApproveMcpAgentPlanUseCase:
    def __init__(self, repository: AgentApprovalRepositoryPort, gateway: AgentToolGatewayPort) -> None:
        self._repository = repository
        self._gateway = gateway

    def execute(self, command: McpApprovalCommand) -> AgentRun:
        current = self._repository.get_current_plan_for_user(
            command.workspace_id, command.user_id, command.run_id
        )
        if current is None:
            raise McpApprovalError(404, "HANDLE_NOT_FOUND", "MCP Agent run was not found.")
        run, plan = current
        if (
            run.origin != "MCP_CLIENT"
            or run.grant_id != command.grant_id
            or run.client_id != command.client_id
            or run.policy_generation != command.policy_generation
        ):
            raise McpApprovalError(409, "IDEMPOTENCY_CONFLICT", "MCP approval identity does not match.")
        if plan.version != command.plan_version or plan.operation_hash != command.operation_hash:
            raise McpApprovalError(409, "SOURCE_CHANGED", "MCP Agent plan changed and must be reviewed again.")
        validate_operation_ids_format(list(command.operation_ids))
        try:
            selected = selected_operation_ids(plan, list(command.operation_ids))
        except AgentPlanSelectionInvalid as exc:
            raise McpApprovalError(400, "INVALID_INPUT", str(exc)) from exc
        selected = selected or tuple(operation.id for operation in plan.operations)
        command = replace(command, operation_ids=selected)
        replay = self._repository.find_mcp_approval(command)
        if replay is not None:
            return replay
        if run.status != "awaiting_approval" or plan.status != "awaiting_approval":
            raise McpApprovalError(409, "INVALID_STATE", "MCP Agent plan is not awaiting approval.")

        try:
            validity = self._gateway.lookup_mcp_approval(command)
        except ToolGatewayError:
            raise McpApprovalError(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "MCP approval reservation could not be verified.",
            ) from None
        if validity.status != "VALID":
            if validity.status == "FENCED" and validity.reason in _HALT_REASONS:
                self._repository.halt_mcp_run(command.run_id, validity.reason)
            status_code = 503 if validity.status == "UNKNOWN" else 404 if validity.status == "NOT_FOUND" else 409
            raise McpApprovalError(
                status_code,
                validity.reason or validity.status,
                "MCP approval reservation is not valid for execution.",
            )
        if validity.lifecycle_status not in {"RESERVED", "FORWARDED"}:
            raise McpApprovalError(409, "INVALID_STATE", "MCP approval lifecycle is not executable.")
        return self._repository.approve_mcp_and_enqueue(command, plan, str(uuid4()))
