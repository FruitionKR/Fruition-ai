from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.skill.application.manage_skill import ManageSkillUseCase
from app.modules.skill.infrastructure import postgres_skill_repository as storage
from app.modules.skill.interfaces.http import routes


@pytest.mark.parametrize("manageable,expected", [(True, 204), (False, 404)])
def test_delete_route_returns_empty_success_or_hides_unmanageable_skill(manageable, expected):
    repository = MagicMock()
    if not manageable:
        repository.delete.side_effect = ValueError("Skill not found or not manageable.")
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_manage_skill_use_case] = lambda: ManageSkillUseCase(repository)
    with TestClient(app) as client:
        response = client.delete("/skills/skill-1", params={"workspace_id": "ws-1", "user_id": "user-1"})
    assert response.status_code == expected
    if manageable:
        assert response.content == b""
    repository.delete.assert_called_once_with("ws-1", "user-1", "skill-1")


@pytest.mark.parametrize("owner", [True, False])
@pytest.mark.parametrize("manageable", [True, False])
def test_delete_checks_actor_under_row_lock_before_mutating(owner, manageable):
    conn = MagicMock()
    with patch.object(storage, "_is_team_owner", return_value=owner), \
         patch.object(storage.database, "connect_ai") as connect, \
         patch.object(storage, "_lock_manageable", return_value={"id": "skill-1"} if manageable else None) as lock:
        connect.return_value.__enter__.return_value = conn
        repository = storage.PostgresSkillRepository()
        if manageable:
            repository.delete("ws-1", "user-1", "skill-1")
            conn.execute.assert_called_once_with("DELETE FROM skills WHERE id = %s", ("skill-1",))
        else:
            with pytest.raises(ValueError, match="not manageable"):
                repository.delete("ws-1", "user-1", "skill-1")
            conn.execute.assert_not_called()
        lock.assert_called_once_with(conn, "ws-1", "user-1", "skill-1", owner)
