import hashlib
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.core.ai_markdown_sanitizer import external_urls
from app.modules.agent_run.application.mcp_proposal import (
    CreateMcpProposalUseCase,
    McpProposalError,
    mcp_proposal_request_hash,
)
from app.modules.agent_run.domain.entities import (
    McpDocumentEditContext,
    McpProposalActor,
    McpProposalCommand,
    McpProposalResult,
)
from app.modules.agent_run.infrastructure.chat_completions_plan_generator import ChatCompletionsPlanGenerator
from app.modules.wiki_generation.infrastructure.chat_completions_llm import ChatCompletionsJsonClient


def _hash(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _command(markdown="new"):
    target = {"type": "selection", "start_line": 2, "end_line": 2}
    operation = {
        "id": "op-1", "sequence": 1, "tool_name": "apply_document_edit",
        "target_type": "document", "target_id": "doc_1", "base_version": 1,
        "source_parent_id": None, "destination_parent_id": None,
        "arguments": {
            "document_id": "doc_1", "base_version": 1, "target": dict(target),
            "content_artifact_id": "artifact-1", "content_hash": _hash(markdown),
        },
        "reason": "Fix text", "depends_on": [],
    }
    command = McpProposalCommand(
        actor=McpProposalActor(
            invocation_id="00000000-0000-4000-8000-000000000001",
            grant_id="00000000-0000-4000-8000-000000000002",
            user_id="user", workspace_id="workspace", client_id="https://client.example/metadata",
            policy_generation=1, origin="MCP_CLIENT", spec_version="2025-11-25",
            tool_name="propose_document_edit",
        ),
        idempotency_key="key", request_hash="",
        proposal={
            "instruction": "Fix text", "request_summary": "Fix text", "target_folder_id": None,
            "operations": [operation],
            "artifacts": [{
                "id": "artifact-1", "purpose": "apply_document_edit", "document_id": "doc_1",
                "base_version": 1, "target": dict(target), "markdown": markdown, "content_hash": _hash(markdown),
            }],
        },
    )
    return replace(command, request_hash=mcp_proposal_request_hash(command))


@pytest.fixture
def proposal():
    context = McpDocumentEditContext(
        document_id="doc_1", workspace_id="workspace", current_version=1,
        content_hash=_hash("# Title\nold\nunchanged"), current_markdown="# Title\nold\nunchanged",
        display_name="Title", parent_folder_id=None,
    )
    reader = Mock()
    reader.read.return_value = context
    repository = Mock()
    repository.find_mcp_proposal.return_value = None
    repository.create_mcp_proposal.side_effect = lambda **kwargs: McpProposalResult(
        kwargs["run"].id, kwargs["plan"].id, kwargs["plan"].version, kwargs["plan"].operation_hash,
    )
    return CreateMcpProposalUseCase(repository, reader), repository, reader


def test_creates_sanitized_approval_plan_without_llm_or_planning_job(proposal, monkeypatch):
    use_case, repository, _reader = proposal
    forbidden = Mock(side_effect=AssertionError("MCP proposal must not call an LLM"))
    monkeypatch.setattr(ChatCompletionsPlanGenerator, "generate", forbidden)
    monkeypatch.setattr(ChatCompletionsJsonClient, "complete_json", forbidden)

    result = use_case.execute(_command("new [link](https://untrusted.example)"))

    saved = repository.create_mcp_proposal.call_args.kwargs
    artifact, run, plan = saved["artifacts"][0], saved["run"], saved["plan"]
    assert result.status == run.status == "awaiting_approval"
    assert (run.origin, run.grant_id, run.user_id, run.workspace_id) == (
        "MCP_CLIENT", saved["actor"].grant_id, "user", "workspace",
    )
    assert artifact.markdown.startswith("# Title\nnew ")
    assert artifact.markdown.endswith("\nunchanged")
    assert not external_urls(artifact.markdown)
    assert artifact.content_hash == _hash(artifact.markdown)
    assert plan.operations[0].arguments["content_hash"] == artifact.content_hash
    assert plan.operations[0].arguments["content_artifact_id"] == artifact.id
    assert json.loads(artifact.preview_json)["removed_lines"] == 1
    repository.create_with_planning_job.assert_not_called()
    forbidden.assert_not_called()


@pytest.mark.parametrize("forgery", ["request_hash", "artifact_hash", "workspace", "version", "target", "parent", "context_hash"])
def test_rejects_forged_bindings_before_creating_artifacts(proposal, forgery):
    use_case, repository, reader = proposal
    command = _command()
    operation, artifact = command.proposal["operations"][0], command.proposal["artifacts"][0]
    if forgery == "request_hash":
        command = replace(command, request_hash="sha256:" + "0" * 64)
    elif forgery == "artifact_hash":
        artifact["content_hash"] = operation["arguments"]["content_hash"] = "sha256:" + "0" * 64
    elif forgery == "target":
        artifact["target"]["end_line"] = operation["arguments"]["target"]["end_line"] = 99
    elif forgery == "parent":
        operation["source_parent_id"] = "forged-parent"
    else:
        field, value = {
            "workspace": ("workspace_id", "other-workspace"),
            "version": ("current_version", 2),
            "context_hash": ("content_hash", "sha256:" + "0" * 64),
        }[forgery]
        reader.read.return_value = replace(reader.read.return_value, **{field: value})
    if forgery != "request_hash":
        command = replace(command, request_hash=mcp_proposal_request_hash(command))
    with pytest.raises(McpProposalError):
        use_case.execute(command)
    repository.create_mcp_proposal.assert_not_called()


def test_rejects_no_op_without_creating_run_or_artifact(proposal):
    use_case, repository, _reader = proposal
    with pytest.raises(McpProposalError) as error:
        use_case.execute(_command("old"))
    assert error.value.code == "OBVIOUS_NO_OP"
    assert error.value.operation_error["operation_id"] == "op-1"
    repository.create_mcp_proposal.assert_not_called()


def test_retry_checks_current_authorization_before_returning_existing_result(proposal):
    use_case, repository, reader = proposal
    repository.find_mcp_proposal.return_value = McpProposalResult("run", "plan", 1, "hash")
    reader.read.side_effect = McpProposalError(403, "PERMISSION_REVOKED", "revoked")
    with pytest.raises(McpProposalError, match="revoked"):
        use_case.execute(_command())
    repository.find_mcp_proposal.assert_not_called()
    repository.create_mcp_proposal.assert_not_called()
