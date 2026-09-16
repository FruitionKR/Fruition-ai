from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.modules.agent_run.application.ports import ToolGatewayError


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
    ) -> dict[str, object]:
        return self._call(
            f"/internal/agent/tools/execute/{tool_name}",
            {
                "run_id": run_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "plan_id": plan_id,
                "plan_version": plan_version,
                "operation_hash": operation_hash,
                "operation_id": operation_id,
                "idempotency_key": idempotency_key,
                "arguments": arguments,
            },
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
            raise ToolGatewayError(exc.code, exc.code == 429 or exc.code >= 500) from exc
        except (URLError, TimeoutError) as exc:
            raise ToolGatewayError(None, True) from exc
        if not isinstance(value, dict):
            raise ToolGatewayError(None, False)
        return value


def build_backend_tool_gateway() -> BackendToolGateway:
    token = os.environ.get("AGENT_INTERNAL_TOKEN")
    if not token:
        raise RuntimeError("Set AGENT_INTERNAL_TOKEN for the Agent worker.")
    return BackendToolGateway(
        os.environ.get("AGENT_BACKEND_URL", "http://document-svc:8080"),
        token,
        int(os.environ.get("AGENT_TOOL_TIMEOUT_SECONDS", "30")),
    )
