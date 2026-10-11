from unittest.mock import Mock, patch

from app.modules.skill.infrastructure.workspace_authorization import get_workspace_role


def test_get_workspace_role_uses_access_internal_api(monkeypatch) -> None:
    monkeypatch.setenv("ACCESS_INTERNAL_BASE_URL", "http://access-svc:8081/")
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "internal-token")
    response = Mock()
    response.json.return_value = {"role": "OWNER"}

    with patch(
        "app.modules.skill.infrastructure.workspace_authorization.httpx.get",
        return_value=response,
    ) as request:
        role = get_workspace_role("workspace/1", "user/1")

    assert role == "OWNER"
    request.assert_called_once_with(
        "http://access-svc:8081/internal/authz/workspaces/workspace%2F1/users/user%2F1",
        headers={"X-Internal-Token": "internal-token"},
        timeout=3.0,
    )
    response.raise_for_status.assert_called_once_with()


def _access_body(monkeypatch, body):
    monkeypatch.setenv("ACCESS_INTERNAL_BASE_URL", "http://access-svc:8081")
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "internal-token")
    response = Mock()
    response.json.return_value = body
    return patch("app.modules.skill.infrastructure.workspace_authorization.httpx.get", return_value=response)


def test_team_skill_management_needs_owner_or_skill_author(monkeypatch) -> None:
    from app.modules.skill.infrastructure.workspace_authorization import can_manage_team_skills

    cases = [
        ({"role": "OWNER", "permissions": []}, True),
        ({"role": "MEMBER", "permissions": ["skill.author"]}, True),
        ({"role": "MEMBER", "permissions": ["wiki.ingest"]}, False),
        # permissions를 내리기 전 access 응답은 권한 없음으로 본다.
        ({"role": "MEMBER"}, False),
        ({"role": "OWNER"}, True),
        ({"role": "MEMBER", "permissions": "skill.author"}, False),
        ({"role": "MEMBER", "permissions": [1, "skill.author"]}, True),
        ({"role": "NONE", "permissions": ["skill.author"]}, False),
    ]
    for body, expected in cases:
        with _access_body(monkeypatch, body):
            assert can_manage_team_skills("ws", "user") is expected, body
