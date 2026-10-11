from __future__ import annotations

import os
from urllib.parse import quote

import httpx


_ALLOWED_ROLES = {"OWNER", "MEMBER", "NONE"}
TEAM_SKILL_PERMISSION = "skill.author"


def get_workspace_role(workspace_id: str, user_id: str) -> str:
    return get_workspace_access(workspace_id, user_id)[0]


def can_manage_team_skills(workspace_id: str, user_id: str) -> bool:
    """팀 Skill 작성·수정·삭제·켜고 끄기는 OWNER나 skill.author 권한을 받은 멤버만 한다(access ADR 0019)."""
    role, permissions = get_workspace_access(workspace_id, user_id)
    return role == "OWNER" or (role == "MEMBER" and TEAM_SKILL_PERMISSION in permissions)


def get_workspace_access(workspace_id: str, user_id: str) -> tuple[str, frozenset[str]]:
    """역할과 권한 항목. access가 permissions를 내리기 전 버전이면 권한은 비어 있다."""
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
        permissions = body.get("permissions") if isinstance(body, dict) else None
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise RuntimeError("Access authorization lookup failed.") from exc

    if role not in _ALLOWED_ROLES:
        raise RuntimeError("Access authorization returned an invalid role.")
    if not isinstance(permissions, list):
        permissions = []
    return role, frozenset(p for p in permissions if isinstance(p, str))
