import os
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import api
from app.modules.agent_run.interfaces.http.dependencies import get_agent_run_repository
from app.modules.agent_run.interfaces.http.routes import (
    authorize_agent_tool_execute,
    get_markdown_agent_run,
    list_agent_artifacts,
    resolve_agent_artifact,
)
from app.modules.agent_run.interfaces.http.schemas import (
    AgentArtifactListRequest,
    AgentArtifactResolveRequest,
    AgentToolExecuteAuthorizationRequest,
)


class ScopedRepository:
    def __init__(self) -> None:
        self.read_authorizations = 0

    def get_markdown_turn_status(self, workspace_id: str, user_id: str, run_id: str):
        if (workspace_id, user_id, run_id) != ("workspace-1", "user-1", "run-1"):
            return None
        return {
            "id": run_id,
            "document_id": "document-1",
            "base_version": 7,
            "apply_operation_id": "op-1",
            "status": "completed",
            "result": {"edit": {"changed": True}},
            "error_code": None,
        }

    def authorize_tool_read(self, workspace_id: str, user_id: str, run_id: str) -> bool:
        self.read_authorizations += 1
        return (workspace_id, user_id, run_id) == ("workspace-1", "user-1", "run-1")

    def authorize_tool_execute(self, **values) -> bool:
        return values["arguments"] == {"document_id": "document-1"}

    def list_artifacts(self, workspace_id: str, user_id: str, run_id: str):
        if (workspace_id, user_id, run_id) != ("workspace-1", "user-1", "run-1"):
            return []
        return [{"id": "artifact-1", "content_hash": "sha256:" + "a" * 64, "purpose": "create_document"}]

    def resolve_artifact(self, **values):
        if (values["workspace_id"], values["user_id"], values["run_id"]) != (
            "workspace-1", "user-1", "run-1"
        ):
            return None
        if values["content_hash"] != "sha256:" + "a" * 64:
            raise ValueError("Agent artifact metadata does not match the approved operation.")
        if values["purpose"] != "create_document" or any(
            values[field] is not None for field in ("document_id", "base_version", "target")
        ):
            raise ValueError("Agent artifact metadata does not match the approved operation.")
        return {
            "id": "artifact-1",
            "content_hash": "sha256:" + "a" * 64,
            "purpose": "create_document",
            "markdown": "# 문서\n",
        }


def test_internal_status_lookup_is_scoped_to_workspace_and_user() -> None:
    response = get_markdown_agent_run(
        "run-1", "workspace-1", "user-1", ScopedRepository()
    )

    assert response.id == "run-1"
    assert response.document_id == "document-1"

    with pytest.raises(HTTPException) as raised:
        get_markdown_agent_run("run-1", "workspace-2", "user-1", ScopedRepository())

    assert raised.value.status_code == 404


class DocumentlessRepository(ScopedRepository):
    """문서를 열지 않은 턴. 편집 대상이 없어 셋 다 비어 있다."""

    def get_markdown_turn_status(self, workspace_id: str, user_id: str, run_id: str):
        return {
            "id": run_id,
            "document_id": None,
            "base_version": None,
            "apply_operation_id": None,
            "status": "completed",
            "result": {"action": "chat_answer"},
            "error_code": None,
        }


def test_internal_status_lookup_allows_run_without_document() -> None:
    response = get_markdown_agent_run(
        "run-1", "workspace-1", "user-1", DocumentlessRepository()
    )

    assert response.id == "run-1"
    assert (response.document_id, response.base_version, response.apply_operation_id) == (None, None, None)
    assert response.status == "completed"


def test_execute_authorization_rejects_argument_mismatch() -> None:
    payload = AgentToolExecuteAuthorizationRequest(
        run_id="run-1",
        workspace_id="workspace-1",
        user_id="user-1",
        plan_id="plan-1",
        plan_version=1,
        operation_hash="a" * 64,
        operation_id="operation-1",
        tool_name="rename_document",
        arguments={"document_id": "tampered"},
    )

    with pytest.raises(HTTPException) as raised:
        authorize_agent_tool_execute(payload, ScopedRepository())

    assert raised.value.status_code == 409


def test_artifact_list_and_resolve_are_scoped() -> None:
    repository = ScopedRepository()
    listed = list_agent_artifacts(
        AgentArtifactListRequest(run_id="run-1", workspace_id="workspace-1", user_id="user-1"),
        repository,
    )
    assert listed[0]["id"] == "artifact-1"

    resolved = resolve_agent_artifact(
        AgentArtifactResolveRequest(
            run_id="run-1",
            workspace_id="workspace-1",
            user_id="user-1",
            artifact_id="artifact-1",
            content_hash="sha256:" + "a" * 64,
            purpose="create_document",
        ),
        repository,
    )
    assert resolved["markdown"] == "# 문서\n"

    with pytest.raises(HTTPException) as raised:
        resolve_agent_artifact(
            AgentArtifactResolveRequest(
                run_id="run-1",
                workspace_id="workspace-2",
                user_id="user-1",
                artifact_id="artifact-1",
                content_hash="sha256:" + "a" * 64,
                purpose="create_document",
            ),
            repository,
        )
    assert raised.value.status_code == 404


def test_artifact_resolve_rejects_hash_or_purpose_mismatch() -> None:
    repository = ScopedRepository()
    with pytest.raises(HTTPException) as raised:
        resolve_agent_artifact(
            AgentArtifactResolveRequest(
                run_id="run-1",
                workspace_id="workspace-1",
                user_id="user-1",
                artifact_id="artifact-1",
                content_hash="sha256:" + "b" * 64,
                purpose="create_document",
            ),
            repository,
        )
    assert raised.value.status_code == 409

    with pytest.raises(HTTPException) as raised:
        resolve_agent_artifact(
            AgentArtifactResolveRequest(
                run_id="run-1",
                workspace_id="workspace-1",
                user_id="user-1",
                artifact_id="artifact-1",
                content_hash="sha256:" + "a" * 64,
                purpose="apply_document_edit",
                document_id="document-1",
                base_version=2,
                target={"type": "whole_document", "start_line": 1, "end_line": 2},
            ),
            repository,
        )
    assert raised.value.status_code == 409


def test_tool_authorization_route_requires_internal_token_before_repository() -> None:
    repository = ScopedRepository()
    previous_overrides = dict(api.app.dependency_overrides)
    api.app.dependency_overrides[get_agent_run_repository] = lambda: repository
    try:
        with (
            patch.dict(os.environ, {"INTERNAL_CALLBACK_TOKEN": "test-internal-token"}),
            patch.object(api.database, "ensure_ai_schema"),
            TestClient(api.app) as client,
        ):
            unauthorized = client.post(
                "/internal/agent/runs/tool-authorizations/read",
                json={
                    "run_id": "run-1",
                    "workspace_id": "workspace-1",
                    "user_id": "user-1",
                },
            )
            authorized = client.post(
                "/internal/agent/runs/tool-authorizations/read",
                headers={"X-Internal-Token": "test-internal-token"},
                json={
                    "run_id": "run-1",
                    "workspace_id": "workspace-1",
                    "user_id": "user-1",
                },
            )
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(previous_overrides)

    assert unauthorized.status_code == 401
    assert authorized.status_code == 204
    assert repository.read_authorizations == 1
