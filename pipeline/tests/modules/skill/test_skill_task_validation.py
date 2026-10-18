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


def test_publish_payload_accepts_missing_allowed_tools():
    from app.modules.skill.interfaces.http.schemas import PublishAuthoredSkillRequest

    payload = PublishAuthoredSkillRequest.model_validate(dict(
        workspace_id="ws", user_id="user", provider="openai", model="gpt-5-nano", scope_type="personal",
        name="example", description="설명", instructions_markdown="문서를 정리한다",
        capabilities=["document-edit"],
    ))

    assert payload.allowed_tools == []


def test_publish_result_mode_mismatch_is_400_with_code_before_llm(monkeypatch):
    from app.modules.skill.application.author_skill import AuthorSkillUseCase
    from app.modules.skill.application.manage_skill import ManageSkillUseCase

    generator = MagicMock()
    use_case = AuthorSkillUseCase(generator, MagicMock(), ManageSkillUseCase(MagicMock()))
    monkeypatch.setattr(routes, "get_author_skill_use_case", lambda **_: use_case)
    monkeypatch.setattr(journal, "execute", MagicMock(side_effect=lambda command, handle: handle()))
    app = FastAPI()
    app.include_router(routes.router)
    payload = dict(workspace_id="ws", user_id="user", provider="openai", model="gpt-5-nano", scope_type="personal",
                   name="example", description="설명", instructions_markdown="문서를 고친다",
                   capabilities=["document-edit"], allowed_tools=["list_root_items", "list_folder_children", "apply_document_edit"],
                   targets=["selection"], result_mode="create")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/skills/tasks", json=dict(
            run_id="mode-test", kind="skill_publish", workspace_id="ws", user_id="user", payload=payload,
        ))

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_result_mode"
    generator.classify.assert_not_called()
    generator.generate.assert_not_called()


def test_legacy_skill_version_rows_read_default_targets():
    from app.modules.skill.infrastructure.postgres_skill_repository import _row_to_version

    def row(capabilities):
        return {"ev_id": "v1", "ev_skill_id": "s1", "ev_version": 1, "ev_name": "example", "ev_description": "설명",
                "ev_instructions_markdown": "지침", "ev_capabilities": capabilities, "ev_allowed_tools": [],
                "ev_targets": [], "ev_result_mode": None, "ev_safety_result": {}, "ev_status": "published",
                "ev_created_by": "user", "ev_created_at": None, "ev_published_at": None}

    assert _row_to_version(row(["document-edit"]), "ev").targets == ("current_document", "selection")
    assert _row_to_version(row(["folder-organize"]), "ev").targets == ()
