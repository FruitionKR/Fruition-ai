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
