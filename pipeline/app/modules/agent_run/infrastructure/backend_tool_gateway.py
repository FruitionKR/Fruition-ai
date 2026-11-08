from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.modules.agent_run.application.ports import ToolGatewayError
from app.modules.agent_run.domain.entities import (
    McpApprovalCommand,
    McpApprovalStatus,
    McpExecutionIdentity,
)


_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_MCP_APPROVAL_STATUSES = {"VALID", "NOT_FOUND", "MISMATCH", "FENCED", "SETTLED", "UNKNOWN"}


class BackendToolGateway:
    def __init__(self, base_url: str, service_token: str, timeout_seconds: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout_seconds = timeout_seconds

    def read(
        self,
        tool_name: str,
        *,
        run_id: str,
        workspace_id: str,
        user_id: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        return self._call(
            f"/internal/agent/tools/read/{tool_name}",
            {
                "run_id": run_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "arguments": arguments,
            },
        )

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
        approval_id: str | None = None,
        attempt_id: str | None = None,
        grant_id: str | None = None,
        client_id: str | None = None,
        policy_generation: int | None = None,
    ) -> dict[str, object]:
        payload = {
            "run_id": run_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "plan_id": plan_id,
            "plan_version": plan_version,
            "operation_hash": operation_hash,
            "operation_id": operation_id,
            "idempotency_key": idempotency_key,
            "arguments": arguments,
        }
        mcp = {
            "approval_id": approval_id,
            "attempt_id": attempt_id,
            "grant_id": grant_id,
            "client_id": client_id,
            "policy_generation": policy_generation,
        }
        if any(value is not None for value in mcp.values()):
            if any(value is None for value in mcp.values()):
                raise ValueError("MCP execution identity must be complete.")
            payload.update(mcp)
        return self._call(
            f"/internal/agent/tools/execute/{tool_name}",
            payload,
        )

    def lookup_mcp_approval(self, command: McpApprovalCommand) -> McpApprovalStatus:
        value = self._call(
            "/internal/agent/mcp/approvals/lookup",
            {
                "approval_id": command.approval_id,
                "run_id": command.run_id,
                "workspace_id": command.workspace_id,
                "user_id": command.user_id,
                "plan_version": command.plan_version,
                "operation_hash": command.operation_hash,
                "operation_ids": list(command.operation_ids),
                "grant_id": command.grant_id,
                "client_id": command.client_id,
                "policy_generation": command.policy_generation,
            },
        )
        expected = {"status", "reason", "lifecycle_status", "forward_status", "settlement_status"}
        if set(value) != expected or value.get("status") not in _MCP_APPROVAL_STATUSES:
            raise ToolGatewayError(None, False)
        if any(value[key] is not None and not isinstance(value[key], str) for key in expected - {"status"}):
            raise ToolGatewayError(None, False)
        return McpApprovalStatus(**value)

    def lookup_mcp_receipt(
        self,
        identity: McpExecutionIdentity,
        operation_id: str,
        workspace_id: str,
        user_id: str,
    ) -> dict[str, object]:
        return self._call(
            "/internal/agent/mcp/receipts/lookup",
            {
                "approval_id": identity.approval_id,
                "operation_id": operation_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "grant_id": identity.grant_id,
                "client_id": identity.client_id,
            },
        )

    def notify_plan_ready(self, *, run_id: str, workspace_id: str, user_id: str) -> None:
        """document-svc의 plan-ready를 부른다. 승인 대기면 자동 승인을 판단하고, 어느 상태든 사용량을 수집한다."""
        self._call(
            f"/internal/agent/runs/{quote(run_id, safe='')}/plan-ready",
            {"workspace_id": workspace_id, "user_id": user_id},
        )

    def _call(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        request = Request(
            self._base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Agent-Service-Token": self._service_token,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            code = None
            try:
                body = exc.read(8193)
                if len(body) <= 8192:
                    error = json.loads(body.decode("utf-8"))
                    candidate = error.get("error", {}).get("code") if isinstance(error, dict) else None
                    if isinstance(candidate, str) and _ERROR_CODE.fullmatch(candidate):
                        code = candidate
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                pass
            raise ToolGatewayError(exc.code, exc.code == 429 or exc.code >= 500, code) from exc
        except (URLError, TimeoutError) as exc:
            raise ToolGatewayError(None, True) from exc
        if not isinstance(value, dict):
            raise ToolGatewayError(None, False)
        return value


def agent_backend_url() -> str:
    """agent_worker 프로세스가 document-svc를 부를 때 쓰는 주소. tool gateway와 폴더 본문 읽기가 함께 쓴다."""
    return os.environ.get("AGENT_BACKEND_URL", "http://document-svc:8080")


def build_backend_tool_gateway() -> BackendToolGateway:
    token = os.environ.get("AGENT_INTERNAL_TOKEN")
    if not token:
        raise RuntimeError("Set AGENT_INTERNAL_TOKEN for the Agent worker.")
    return BackendToolGateway(
        agent_backend_url(),
        token,
        int(os.environ.get("AGENT_TOOL_TIMEOUT_SECONDS", "30")),
    )
