import copy
import hashlib
import json
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import api
from app.modules.agent_run.application.mcp_proposal import (
    CreateMcpProposalUseCase,
    McpProposalError,
    mcp_proposal_request_hash,
)
from app.modules.agent_run.domain.entities import (
    McpDocumentEditContext,
    McpPlanContext,
    McpProposalResult,
)
from app.modules.agent_run.interfaces.http.dependencies import get_create_mcp_proposal_use_case
from app.modules.agent_run.interfaces.http.schemas import McpAgentProposalRequest


RAW_HASH = "sha256:2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
GOLDEN_HASH = "sha256:9e02041dfc0c922640e56e612b235890e99d3b1074d13a67893fdfb577725c03"
TRAILING_HASH = "sha256:5c959495edcb3e8504cd4b8767b390baa7e21328819f1ab0bee12dc81a90be2f"


def proposal_body() -> dict[str, object]:
    return {
        "schema_version": "mcp.agent-proposal.v1",
        "actor": {
            "invocation_id": "00000000-0000-4000-8000-000000000002",
            "grant_id": "00000000-0000-4000-8000-000000000001",
            "user_id": "user-1",
            "workspace_id": "workspace-1",
            "client_id": "https://client.example/metadata",
            "policy_generation": 1,
            "origin": "MCP_CLIENT",
            "spec_version": "2025-11-25",
            "tool_name": "propose_document_edit",
        },
        "idempotency_key": "request-1",
        "request_hash": GOLDEN_HASH,
        "proposal": {
            "instruction": "Fix title",
            "request_summary": "Fix title",
            "target_folder_id": None,
            "operations": [{
                "id": "op-1",
                "sequence": 1,
                "tool_name": "apply_document_edit",
                "target_type": "document",
                "target_id": "doc_1",
                "base_version": 1,
                "source_parent_id": None,
                "destination_parent_id": None,
                "arguments": {
                    "document_id": "doc_1",
                    "base_version": 1,
                    "target": {"type": "selection", "start_line": 1, "end_line": 1},
                    "content_artifact_id": "artifact-1",
                    "content_hash": RAW_HASH,
                },
                "reason": "Fix title",
                "depends_on": [],
            }],
            "artifacts": [{
                "id": "artifact-1",
                "content_hash": RAW_HASH,
                "purpose": "apply_document_edit",
                "document_id": "doc_1",
                "base_version": 1,
                "target": {"type": "selection", "start_line": 1, "end_line": 1},
                "markdown": "x",
            }],
        },
    }


def command(body: dict[str, object] | None = None):
    return McpAgentProposalRequest.model_validate(body or proposal_body()).to_application()


def plan_body(operation_count: int = 1) -> dict[str, object]:
    body = proposal_body()
    body["actor"]["tool_name"] = "propose_plan"
    operations = []
    for sequence in range(1, operation_count + 1):
        operations.append({
            "id": f"op-{sequence}", "sequence": sequence, "tool_name": "create_folder",
            "target_type": "folder", "target_id": None, "base_version": None,
            "source_parent_id": None, "destination_parent_id": None,
            "arguments": {"name": f"Folder {sequence}", "parent_folder_id": None},
            "reason": f"Create folder {sequence}", "depends_on": [],
        })
    body["proposal"] = {
        "instruction": None, "request_summary": "Create folders", "target_folder_id": None,
        "operations": operations, "artifacts": [],
    }
    body["request_hash"] = "sha256:" + "0" * 64
    return body


class MemoryRepository:
    def __init__(self) -> None:
        self.created = []
        self.existing = None

    def find_mcp_proposal(self, actor, idempotency_key, request_hash):
        return self.existing

    def create_mcp_proposal(self, **values):
        self.created.append(values)
        plan = values["plan"]
        return McpProposalResult(values["run"].id, plan.id, plan.version, plan.operation_hash)


class ContextReader:
    def __init__(self, markdown: str = "old") -> None:
        self.context = McpDocumentEditContext(
            document_id="doc_1",
            workspace_id="workspace-1",
            current_version=1,
            content_hash="sha256:" + hashlib.sha256(markdown.encode()).hexdigest(),
            current_markdown=markdown,
            display_name="Document",
            parent_folder_id=None,
        )
        self.calls = 0
        self.plan_context = McpPlanContext("workspace-1", None, (), ())

    def read(self, actor, document_id, base_version):
        self.calls += 1
        return self.context

    def read_plan(self, actor, target_folder_id, resources, edit_documents):
        self.calls += 1
        return replace(self.plan_context, workspace_id=actor.workspace_id, target_folder_id=target_folder_id)


def test_golden_request_hash_preserves_raw_instruction_and_excludes_invocation() -> None:
    original = command()
    assert mcp_proposal_request_hash(original) == GOLDEN_HASH

    changed_invocation = replace(original, actor=replace(original.actor, invocation_id=str(uuid4())))
    assert mcp_proposal_request_hash(changed_invocation) == GOLDEN_HASH

    trailing = copy.deepcopy(proposal_body())
    trailing["proposal"]["instruction"] = "Fix title "
    trailing["request_hash"] = TRAILING_HASH
    assert mcp_proposal_request_hash(command(trailing)) == TRAILING_HASH


def test_authoritative_context_is_checked_before_retry_and_source_forgery_is_rejected() -> None:
    repository = MemoryRepository()
    repository.existing = McpProposalResult("run", "plan", 1, "a" * 64)
    context = ContextReader()
    use_case = CreateMcpProposalUseCase(repository, context)

    assert use_case.execute(command()).run_id == "run"
    assert context.calls == 1

    context.context = replace(context.context, workspace_id="other")
    with pytest.raises(McpProposalError) as raised:
        use_case.execute(command())
    assert raised.value.code == "SOURCE_CHANGED"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["proposal"]["operations"][0].update(base_version=2),
        lambda body: body["proposal"]["operations"][0]["arguments"].update(content_hash="sha256:" + "0" * 64),
        lambda body: body["proposal"]["operations"][0]["arguments"]["target"].update(end_line=2),
    ],
)
def test_base_artifact_hash_and_range_forgery_are_rejected(mutate) -> None:
    body = proposal_body()
    mutate(body)
    body["request_hash"] = mcp_proposal_request_hash(command({**body, "request_hash": GOLDEN_HASH}))
    with pytest.raises(McpProposalError) as raised:
        CreateMcpProposalUseCase(MemoryRepository(), ContextReader()).execute(command(body))
    assert raised.value.code == "SOURCE_CHANGED"


def test_sanitized_bytes_are_the_stored_previewed_and_operation_hashed_bytes() -> None:
    body = proposal_body()
    markdown = "[keep](https://safe.example)"
    replacement = "[keep](https://safe.example) [drop](https://evil.example)"
    raw_hash = "sha256:" + hashlib.sha256(replacement.encode()).hexdigest()
    proposal = body["proposal"]
    proposal["artifacts"][0].update(markdown=replacement, content_hash=raw_hash)
    proposal["operations"][0]["arguments"]["content_hash"] = raw_hash
    body["request_hash"] = mcp_proposal_request_hash(command(body))
    repository = MemoryRepository()

    CreateMcpProposalUseCase(repository, ContextReader(markdown)).execute(command(body))

    stored = repository.created[0]["artifacts"][0]
    operation = repository.created[0]["plan"].operations[0]
    assert "https://safe.example" in stored.markdown
    assert "https://evil.example" not in stored.markdown
    assert stored.content_hash == "sha256:" + hashlib.sha256(stored.markdown.encode()).hexdigest()
    assert operation.arguments["content_hash"] == stored.content_hash
    assert stored.markdown in json.dumps(json.loads(stored.preview_json), ensure_ascii=False)


def test_obvious_noop_persists_nothing() -> None:
    repository = MemoryRepository()
    with pytest.raises(McpProposalError) as raised:
        CreateMcpProposalUseCase(repository, ContextReader("x")).execute(command())
    assert raised.value.code == "OBVIOUS_NO_OP"
    assert raised.value.operation_error == {
        "operation_id": "op-1",
        "sequence": 1,
        "tool_name": "apply_document_edit",
        "reason_code": "UNCHANGED_TARGET",
    }
    assert repository.created == []


class CapturingUseCase:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _command):
        self.calls += 1
        return McpProposalResult("run", "plan", 1, "a" * 64)


def test_http_requires_service_token_and_rejects_unknown_duplicate_nan_and_invalid_utf8() -> None:
    use_case = CapturingUseCase()
    previous = dict(api.app.dependency_overrides)
    api.app.dependency_overrides[get_create_mcp_proposal_use_case] = lambda: use_case
    try:
        with (
            patch.dict("os.environ", {"AGENT_INTERNAL_TOKEN": "service-token"}),
            patch.object(api.database, "ensure_ai_schema"),
            TestClient(api.app) as client,
        ):
            encoded = json.dumps(proposal_body()).encode()
            assert client.post("/agent/runs/mcp-proposals", content=encoded).status_code == 401
            assert client.post(
                "/agent/runs/mcp-proposals",
                content=encoded,
                headers={"X-Agent-Service-Token": "service-token"},
            ).status_code == 200
            for bad in (
                encoded[:-1] + b',"unknown":1}',
                encoded[:-1] + b',"actor":{}}',
                encoded.replace(b'"policy_generation": 1', b'"policy_generation": NaN'),
                encoded.replace(b'"policy_generation": 1', b'"policy_generation": 9223372036854775808'),
                b'\xff',
            ):
                response = client.post(
                    "/agent/runs/mcp-proposals",
                    content=bad,
                    headers={"X-Agent-Service-Token": "service-token"},
                )
                assert response.status_code == 400
                assert response.json()["error"]["code"] == "INVALID_INPUT"
    finally:
        api.app.dependency_overrides = previous
    assert use_case.calls == 1


def test_http_accepts_exact_propose_plan_shape() -> None:
    body = plan_body()
    use_case = CapturingUseCase()
    previous = dict(api.app.dependency_overrides)
    api.app.dependency_overrides[get_create_mcp_proposal_use_case] = lambda: use_case
    try:
        with (
            patch.dict("os.environ", {"AGENT_INTERNAL_TOKEN": "service-token"}),
            patch.object(api.database, "ensure_ai_schema"),
            TestClient(api.app) as client,
        ):
            response = client.post(
                "/agent/runs/mcp-proposals",
                json=body,
                headers={"X-Agent-Service-Token": "service-token"},
            )
    finally:
        api.app.dependency_overrides = previous
    assert response.status_code == 200
    assert use_case.calls == 1


def test_plan_schema_accepts_one_to_twenty_and_rejects_twenty_one() -> None:
    for count in (1, 20):
        assert len(McpAgentProposalRequest.model_validate(plan_body(count)).proposal.operations) == count
    with pytest.raises(Exception):
        McpAgentProposalRequest.model_validate(plan_body(21))


def test_plan_proposal_is_normalized_without_a_planning_job() -> None:
    body = plan_body(2)
    value = command(body)
    body["request_hash"] = mcp_proposal_request_hash(value)
    repository, context = MemoryRepository(), ContextReader()

    result = CreateMcpProposalUseCase(repository, context).execute(command(body))

    assert result.status == "awaiting_approval"
    assert [operation.sequence for operation in repository.created[0]["plan"].operations] == [1, 2]
    assert repository.created[0]["artifacts"] == ()
    assert context.calls == 1


def test_plan_obvious_rename_noop_rejects_everything_before_persistence() -> None:
    folder_id = "00000000-0000-4000-8000-000000000010"
    body = plan_body()
    body["proposal"]["operations"] = [{
        "id": "rename-1", "sequence": 1, "tool_name": "rename_folder",
        "target_type": "folder", "target_id": folder_id, "base_version": 1,
        "source_parent_id": None, "destination_parent_id": None,
        "arguments": {"folder_id": folder_id, "name": " Existing ", "base_version": 1},
        "reason": "Rename", "depends_on": [],
    }]
    value = command(body)
    body["request_hash"] = mcp_proposal_request_hash(value)
    repository, context = MemoryRepository(), ContextReader()
    context.plan_context = McpPlanContext(
        "workspace-1", None,
        ({"id": folder_id, "type": "folder", "current_version": 1,
          "parent_id": None, "name": "existing", "position": 0},), (),
    )

    with pytest.raises(McpProposalError) as raised:
        CreateMcpProposalUseCase(repository, context).execute(command(body))
    assert raised.value.code == "OBVIOUS_NO_OP"
    assert raised.value.operation_error["operation_id"] == "rename-1"
    assert repository.created == []


def test_plan_duplicate_name_is_warning_not_submission_failure() -> None:
    body = plan_body()
    body["proposal"]["operations"][0]["arguments"]["name"] = "existing"
    value = command(body)
    body["request_hash"] = mcp_proposal_request_hash(value)
    repository, context = MemoryRepository(), ContextReader()
    context.plan_context = McpPlanContext(
        "workspace-1", None,
        ({"id": "00000000-0000-4000-8000-000000000011", "type": "folder",
          "current_version": 1, "parent_id": None, "name": "Existing", "position": 0},), (),
    )

    CreateMcpProposalUseCase(repository, context).execute(command(body))

    operation = repository.created[0]["plan"].operations[0]
    assert operation.warning["type"] == "duplicate_name"
    assert operation.suggested_selected is False


def test_plan_duplicate_content_and_dependents_are_deselected_not_rejected() -> None:
    body = plan_body()
    markdown = "The same sufficiently long document body for deterministic review."
    digest = "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()
    operations = []
    artifacts = []
    for sequence, name in ((1, "First"), (2, "Second")):
        artifact_id = f"artifact-{sequence}"
        operations.append({
            "id": f"op-{sequence}", "sequence": sequence, "tool_name": "create_document",
            "target_type": "document", "target_id": None, "base_version": None,
            "source_parent_id": None, "destination_parent_id": None,
            "arguments": {"display_name": name, "folder_id": None,
                          "content_artifact_id": artifact_id, "content_hash": digest},
            "reason": "Create", "depends_on": [],
        })
        artifacts.append({
            "id": artifact_id, "content_hash": digest, "purpose": "create_document",
            "document_id": None, "base_version": None, "target": None, "markdown": markdown,
        })
    operations.append({
        "id": "op-3", "sequence": 3, "tool_name": "create_folder",
        "target_type": "folder", "target_id": None, "base_version": None,
        "source_parent_id": None, "destination_parent_id": None,
        "arguments": {"name": "After duplicate", "parent_folder_id": None},
        "reason": "Create dependent", "depends_on": ["op-2"],
    })
    body["proposal"]["operations"] = operations
    body["proposal"]["artifacts"] = artifacts
    body["request_hash"] = mcp_proposal_request_hash(command(body))
    repository = MemoryRepository()

    CreateMcpProposalUseCase(repository, ContextReader()).execute(command(body))

    saved = repository.created[0]["plan"].operations
    assert saved[1].warning["type"] == "duplicate_content" and not saved[1].suggested_selected
    assert saved[2].warning["type"] == "depends_on_unselected" and not saved[2].suggested_selected
    preview = json.loads(repository.created[0]["artifacts"][0].preview_json)
    assert preview["removed_lines"] == 0
    assert preview["added_lines"] > 0
