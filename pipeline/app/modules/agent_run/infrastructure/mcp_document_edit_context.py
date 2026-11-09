from __future__ import annotations

from dataclasses import asdict
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.modules.agent_run.application.mcp_proposal import McpProposalError
from app.modules.agent_run.domain.entities import (
    MAX_ARTIFACT_BYTES,
    McpDocumentEditContext,
    McpPlanContext,
    McpPlanEditDocument,
    McpProposalActor,
)
from app.modules.agent_run.infrastructure.backend_tool_gateway import agent_backend_url


_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_RESPONSE_KEYS = {
    "schema_version",
    "document_id",
    "workspace_id",
    "current_version",
    "content_hash",
    "current_markdown",
    "display_name",
    "parent_folder_id",
}


class BackendMcpDocumentEditContextReader:
    def __init__(self, base_url: str, service_token: str, timeout_seconds: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout_seconds = timeout_seconds

    def read(
        self, actor: McpProposalActor, document_id: str, base_version: int
    ) -> McpDocumentEditContext:
        payload = {
            "schema_version": "mcp.document-edit-context.v1",
            "actor": asdict(actor),
            "document_id": document_id,
            "base_version": base_version,
        }
        request = Request(
            self._base_url + "/internal/agent/mcp/document-edit-context",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Agent-Service-Token": self._service_token},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_ARTIFACT_BYTES + 65_537)
        except HTTPError as exc:
            code = _error_code(exc)
            status = exc.code if exc.code in {400, 403, 404, 409, 413} else 503
            raise McpProposalError(
                status,
                code if status != 503 else "DEPENDENCY_UNAVAILABLE",
                "Document edit context was rejected.",
                retryable=status == 503,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise McpProposalError(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "Document edit context is unavailable.",
                retryable=True,
            ) from exc
        if len(raw) > MAX_ARTIFACT_BYTES + 65_536:
            raise McpProposalError(413, "RESPONSE_LIMIT_EXCEEDED", "Document edit context is too large.")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise McpProposalError(503, "DEPENDENCY_UNAVAILABLE", "Document edit context is invalid.") from exc
        if not _valid_response(value, actor, document_id, base_version):
            raise McpProposalError(503, "DEPENDENCY_UNAVAILABLE", "Document edit context is invalid.")
        return McpDocumentEditContext(
            document_id=value["document_id"],
            workspace_id=value["workspace_id"],
            current_version=value["current_version"],
            content_hash=value["content_hash"],
            current_markdown=value["current_markdown"],
            display_name=value["display_name"],
            parent_folder_id=value["parent_folder_id"],
        )

    def read_plan(
        self,
        actor: McpProposalActor,
        target_folder_id: str | None,
        resources: tuple[dict[str, object], ...],
        edit_documents: tuple[dict[str, object], ...],
    ) -> McpPlanContext:
        value = self._post(
            "/internal/agent/mcp/plan-context",
            {
                "schema_version": "mcp.plan-context.v1",
                "actor": asdict(actor),
                "target_folder_id": target_folder_id,
                "resources": list(resources),
                "edit_documents": list(edit_documents),
            },
            "Plan context",
        )
        if not _valid_plan_response(value, actor, target_folder_id):
            raise McpProposalError(503, "DEPENDENCY_UNAVAILABLE", "Plan context is invalid.")
        return McpPlanContext(
            workspace_id=value["workspace_id"],
            target_folder_id=value["target_folder_id"],
            hierarchy=tuple(value["hierarchy"]),
            edit_documents=tuple(McpPlanEditDocument(**item) for item in value["edit_documents"]),
        )

    def _post(self, path: str, payload: dict[str, object], label: str) -> object:
        request = Request(
            self._base_url + path,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Agent-Service-Token": self._service_token},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_ARTIFACT_BYTES * 20 + 65_537)
        except HTTPError as exc:
            code = _error_code(exc)
            status = exc.code if exc.code in {400, 403, 404, 409, 413} else 503
            raise McpProposalError(
                status, code if status != 503 else "DEPENDENCY_UNAVAILABLE",
                f"{label} was rejected.", retryable=status == 503,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise McpProposalError(
                503, "DEPENDENCY_UNAVAILABLE", f"{label} is unavailable.", retryable=True,
            ) from exc
        if len(raw) > MAX_ARTIFACT_BYTES * 20 + 65_536:
            raise McpProposalError(413, "RESPONSE_LIMIT_EXCEEDED", f"{label} is too large.")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise McpProposalError(503, "DEPENDENCY_UNAVAILABLE", f"{label} is invalid.") from exc


def build_mcp_document_edit_context_reader() -> BackendMcpDocumentEditContextReader:
    token = os.environ.get("AGENT_INTERNAL_TOKEN")
    if not token:
        raise RuntimeError("Set AGENT_INTERNAL_TOKEN for MCP proposal context reads.")
    return BackendMcpDocumentEditContextReader(
        agent_backend_url(),
        token,
        int(os.environ.get("AGENT_TOOL_TIMEOUT_SECONDS", "30")),
    )


def _valid_response(value, actor, document_id, base_version) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _RESPONSE_KEYS
        and value["schema_version"] == "mcp.document-edit-context.v1"
        and value["document_id"] == document_id
        and value["workspace_id"] == actor.workspace_id
        and type(value["current_version"]) is int
        and value["current_version"] == base_version
        and isinstance(value["content_hash"], str)
        and _HASH.fullmatch(value["content_hash"]) is not None
        and isinstance(value["current_markdown"], str)
        and isinstance(value["display_name"], str)
        and (value["parent_folder_id"] is None or isinstance(value["parent_folder_id"], str))
    )


def _valid_plan_response(value, actor, target_folder_id) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "workspace_id", "target_folder_id", "hierarchy", "edit_documents"}
        or value.get("schema_version") != "mcp.plan-context.v1"
        or value.get("workspace_id") != actor.workspace_id
        or value.get("target_folder_id") != target_folder_id
        or not isinstance(value.get("hierarchy"), list)
        or not isinstance(value.get("edit_documents"), list)
    ):
        return False
    hierarchy_keys = {"id", "type", "current_version", "parent_id", "name", "position"}
    edit_keys = {
        "document_id", "current_version", "content_hash", "current_markdown",
        "display_name", "parent_folder_id",
    }
    return all(
        isinstance(item, dict)
        and set(item) == hierarchy_keys
        and isinstance(item.get("id"), str)
        and item.get("type") in {"folder", "document"}
        and type(item.get("current_version")) is int
        and item["current_version"] > 0
        and (item.get("parent_id") is None or isinstance(item.get("parent_id"), str))
        and isinstance(item.get("name"), str)
        and (item.get("position") is None or type(item.get("position")) is int)
        for item in value["hierarchy"]
    ) and all(
        isinstance(item, dict)
        and set(item) == edit_keys
        and isinstance(item.get("document_id"), str)
        and type(item.get("current_version")) is int
        and item["current_version"] > 0
        and isinstance(item.get("content_hash"), str)
        and _HASH.fullmatch(item["content_hash"]) is not None
        and isinstance(item.get("current_markdown"), str)
        and isinstance(item.get("display_name"), str)
        and (item.get("parent_folder_id") is None or isinstance(item.get("parent_folder_id"), str))
        for item in value["edit_documents"]
    )


def _error_code(exc: HTTPError) -> str:
    try:
        value = json.loads(exc.read(MAX_ARTIFACT_BYTES + 1))
        code = value.get("error", {}).get("code")
        return code if isinstance(code, str) else "DEPENDENCY_UNAVAILABLE"
    except Exception:
        return "DEPENDENCY_UNAVAILABLE"
