from __future__ import annotations

import os
from urllib.parse import quote

import httpx


_ALLOWED_ROLES = {"OWNER", "MEMBER", "NONE"}


def get_workspace_role(workspace_id: str, user_id: str) -> str:
    base_url = os.environ.get("ACCESS_INTERNAL_BASE_URL", "").rstrip("/")
    internal_token = os.environ.get("INTERNAL_CALLBACK_TOKEN", "")
    if not base_url or not internal_token:
        raise RuntimeError("Access authorization service is not configured.")

    url = (
        f"{base_url}/internal/authz/workspaces/{quote(workspace_id, safe='')}/"
        f"users/{quote(user_id, safe='')}"
    )
    try:
        response = httpx.get(
            url,
            headers={"X-Internal-Token": internal_token},
            timeout=float(os.environ.get("ACCESS_AUTHZ_TIMEOUT_SECONDS", "3")),
        )
        response.raise_for_status()
        body = response.json()
        role = body.get("role") if isinstance(body, dict) else None
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise RuntimeError("Access authorization lookup failed.") from exc

    if role not in _ALLOWED_ROLES:
        raise RuntimeError("Access authorization returned an invalid role.")
    return role
