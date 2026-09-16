from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.skill.interfaces.http import routes
from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal


@pytest.mark.parametrize("kind,invalid,field", [
    ("skill_author", {"instruction": ""}, "instruction"),
    ("skill_publish", {"allowed_tools": ["not_a_tool"]}, "allowed_tools"),
    ("skill_publish", {"capabilities": ["not_a_capability"]}, "capabilities"),
    ("skill_update", {"instructions_markdown": ""}, "instructions_markdown"),
])
def test_invalid_skill_payload_is_422_before_task_execution(monkeypatch, kind, invalid, field):
    payload = dict(workspace_id="ws", user_id="user", provider="openai", model="gpt-5-nano",
                   name="example", description="설명")
    if kind == "skill_author":
        payload.update(scope_type="personal", instruction="문서를 정리한다")
    else:
        payload.update(instructions_markdown="문서를 정리한다")
        if kind == "skill_publish":
            payload.update(scope_type="personal", capabilities=["document-create"], allowed_tools=["create_document"])
    execute = MagicMock(side_effect=lambda command, handle: handle())
    monkeypatch.setattr(journal, "execute", execute)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/skills/tasks", json=dict(
            run_id="validation-test", kind=kind, workspace_id="ws", user_id="user",
            skill_id="skill" if kind == "skill_update" else None, payload={**payload, **invalid},
        ))
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"][:3] == ["body", "payload", field]
    execute.assert_not_called()
