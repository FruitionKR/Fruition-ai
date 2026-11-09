from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.modules.agent_run.infrastructure.backend_tool_gateway import agent_backend_url


_STATUSES = {"VALID", "FENCED", "INVALID", "UNKNOWN", "NOT_FOUND"}
_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_RESPONSE_BYTES = 8_192


class BackendWikiApprovalAuthority:
    def __init__(self, base_url: str, service_token: str, timeout_seconds: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout_seconds = timeout_seconds

    def lookup(self, payload: dict) -> dict[str, str | None]:
        request = Request(
            self._base_url + "/internal/mcp/wiki/approvals/lookup",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Agent-Service-Token": self._service_token,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read(_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError):
            return _unknown()
        if len(raw) > _RESPONSE_BYTES:
            return _unknown()
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _unknown()
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "status", "reason"}
            or value.get("schema_version") != "mcp.wiki-approval-status.v1"
            or not isinstance(value.get("status"), str)
            or value.get("status") not in _STATUSES
            or (
                value.get("reason") is not None
                and (not isinstance(value["reason"], str) or _REASON.fullmatch(value["reason"]) is None)
            )
            or (value["status"] == "VALID" and value.get("reason") is not None)
        ):
            return _unknown()
        return value


class ConfiguredWikiApprovalAuthority:
    def lookup(self, payload: dict) -> dict[str, str | None]:
        token = os.environ.get("AGENT_INTERNAL_TOKEN", "").strip()
        try:
            timeout = int(os.environ.get("MCP_WIKI_APPROVAL_LOOKUP_TIMEOUT_SECONDS", ""))
            if not token or timeout <= 0:
                raise ValueError
        except ValueError:
            return _unknown()
        return BackendWikiApprovalAuthority(agent_backend_url(), token, timeout).lookup(payload)


def _unknown() -> dict[str, str]:
    return {
        "schema_version": "mcp.wiki-approval-status.v1",
        "status": "UNKNOWN",
        "reason": "AUTHZ_UNAVAILABLE",
    }
