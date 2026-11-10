"""document가 보내는 `jev_allowed`는 기본 false이고 request hash·멱등성에 들어가지 않는다."""
from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.mcp_wiki_draft.application import McpWikiDraftIntake, canonical_hash, request_actor
from app.modules.mcp_wiki_draft.interfaces.http.schemas import FinalizeIngestRequest, SubmitLintDraftRequest

HASH = "sha256:" + "0" * 64


def _actor(tool: str) -> dict:
    return {
        "invocation_id": str(uuid4()), "grant_id": str(uuid4()), "user_id": "user-1",
        "workspace_id": "workspace-1", "client_id": "https://client.example/metadata",
        "policy_generation": 1, "origin": "MCP_CLIENT", "spec_version": "2025-11-25", "tool_name": tool,
    }


def _finalize(**changes) -> dict:
    return {
        "schema_version": "mcp.wiki-draft.v1", "actor": _actor("finalize_ingest"), "idempotency_key": "key",
        "request_hash": HASH, "snapshot_id": str(uuid4()), "expected_submitted_window_count": 1, **changes,
    }


def _lint(**changes) -> dict:
    return {
        "schema_version": "mcp.wiki-draft.v1", "actor": _actor("submit_lint_changes"), "idempotency_key": "key",
        "request_hash": HASH, "target_snapshot": [{"page_id": "page-1", "version": 1, "content_hash": HASH}],
        "source_snapshots": [], "operations": [], **changes,
    }


@pytest.mark.parametrize(("model", "body"), [(FinalizeIngestRequest, _finalize), (SubmitLintDraftRequest, _lint)])
def test_jev_allowed_defaults_false_and_requires_json_boolean(model, body):
    assert model.model_validate(body()).jev_allowed is False
    assert model.model_validate(body(jev_allowed=True)).jev_allowed is True
    with pytest.raises(ValidationError):
        model.model_validate(body(jev_allowed="true"))


class _ReplayRepository:
    def __init__(self) -> None:
        self.hashes: list[str] = []

    def find_lint(self, payload, request_hash):
        self.hashes.append(request_hash)
        return {"replayed": True}


@pytest.mark.parametrize("jev_allowed", [False, True])
def test_lint_request_hash_ignores_jev_allowed(jev_allowed):
    payload = SubmitLintDraftRequest.model_validate(_lint())
    command = {
        "schema_version": payload.schema_version, "actor": request_actor(payload.actor),
        "target_snapshot": [item.model_dump() for item in payload.target_snapshot],
        "source_snapshots": [], "operations": [],
    }
    payload = payload.model_copy(update={"request_hash": canonical_hash(command), "jev_allowed": jev_allowed})
    repository = _ReplayRepository()

    assert McpWikiDraftIntake(repository, source_reader=None).lint(payload) == {"replayed": True}
    assert repository.hashes == [canonical_hash(command)]
