from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.mcp_wiki_draft.application import McpWikiDraftError
from app.modules.mcp_wiki_draft.apply_application import ApplyClaim, McpWikiDraftApply
from app.modules.mcp_wiki_draft.infrastructure import apply_authority
from app.modules.mcp_wiki_draft.infrastructure import apply_repository
from app.modules.mcp_wiki_draft.infrastructure.apply_authority import BackendWikiApprovalAuthority
from app.modules.mcp_wiki_draft.interfaces.http.apply_schemas import ApplyWikiDraftRequest


def _payload(**changes) -> ApplyWikiDraftRequest:
    value = {
        "schema_version": "mcp.wiki-apply.v1",
        "actor": {
            "invocation_id": str(uuid4()),
            "grant_id": str(uuid4()),
            "user_id": "user-1",
            "workspace_id": "workspace-1",
            "client_id": "https://client.example/metadata",
            "policy_generation": 3,
            "origin": "MCP_CLIENT",
            "spec_version": "2025-11-25",
            "tool_name": "apply_wiki_draft",
        },
        "approval_id": str(uuid4()),
        "operation_id": "document-operation-1",
        "draft_id": str(uuid4()),
        "draft_version": 1,
        "draft_hash": "sha256:" + "1" * 64,
        "public_state_hash": "sha256:" + "2" * 64,
        "approval_mode": "EXPLICIT_WEB",
    }
    value.update(changes)
    return ApplyWikiDraftRequest.model_validate(value)


class Repository:
    def __init__(self) -> None:
        self.request_hashes = []
        self.stored = None
        self.finished = []

    @contextmanager
    def execution_lock(self, approval_id, operation_id):
        yield

    def claim(self, payload, request_hash):
        self.request_hashes.append(request_hash)
        return ApplyClaim("run-1", {"approval_id": payload.approval_id}, self.stored)

    def publish(self, claim, payload):
        return {"state": "PUBLISHED", "approval_id": payload.approval_id}

    def finish_without_publication(self, claim, payload, state, error_code):
        self.finished.append((state, error_code))
        return {"state": state, "error_code": error_code}

    def result(self, approval_id, operation_id, workspace_id, user_id):
        return {"state": "NOT_FOUND"}


class Authority:
    def __init__(self, status="VALID", reason=None) -> None:
        self.status = status
        self.reason = reason
        self.calls = 0

    def lookup(self, request):
        self.calls += 1
        return {"status": self.status, "reason": self.reason}


def test_exact_apply_schema_and_invocation_only_retry_exclusion():
    payload = _payload()
    repository = Repository()
    authority = Authority()
    use_case = McpWikiDraftApply(repository, authority)
    assert use_case.apply(payload.draft_id, payload)["state"] == "PUBLISHED"
    retry = payload.model_copy(update={
        "actor": payload.actor.model_copy(update={"invocation_id": str(uuid4())})
    })
    assert use_case.apply(payload.draft_id, retry)["state"] == "PUBLISHED"
    assert repository.request_hashes[0] == repository.request_hashes[1]
    with pytest.raises(ValidationError):
        ApplyWikiDraftRequest.model_validate(payload.model_dump() | {"trace_id": "not-body"})
    with pytest.raises(ValidationError):
        _payload(actor=payload.actor.model_dump() | {"client_id": "http://client.example"})


@pytest.mark.parametrize(
    ("authority_status", "reason", "result_state"),
    [
        ("UNKNOWN", "AUTHZ_UNAVAILABLE", "UNKNOWN"),
        ("FENCED", "ADMIN_REVOKED", "HALTED"),
        ("INVALID", "SOURCE_CHANGED", "CONFLICTED"),
        ("NOT_FOUND", None, "CONFLICTED"),
    ],
)
def test_authority_statuses_never_publish(authority_status, reason, result_state):
    payload = _payload()
    repository = Repository()
    result = McpWikiDraftApply(repository, Authority(authority_status, reason)).apply(
        payload.draft_id, payload
    )
    assert result["state"] == result_state
    assert repository.finished[-1][0] == result_state


def test_stored_result_bypasses_authority_and_path_body_must_match():
    payload = _payload()
    repository = Repository()
    repository.stored = {"state": "NO_CHANGE", "changed": False}
    authority = Authority("UNKNOWN", "AUTHZ_UNAVAILABLE")
    use_case = McpWikiDraftApply(repository, authority)
    assert use_case.apply(payload.draft_id, payload) == repository.stored
    assert authority.calls == 0
    with pytest.raises(McpWikiDraftError) as mismatch:
        use_case.apply(str(uuid4()), payload)
    assert mismatch.value.code == "IDEMPOTENCY_CONFLICT"


def test_approval_lookup_uses_exact_route_header_body_and_fails_closed(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({
                "schema_version": "mcp.wiki-approval-status.v1",
                "status": "VALID",
                "reason": None,
            }).encode()

    def fake_urlopen(request, timeout):
        captured.update({
            "url": request.full_url,
            "token": request.get_header("X-agent-service-token"),
            "body": json.loads(request.data),
            "timeout": timeout,
        })
        return Response()

    monkeypatch.setattr(apply_authority, "urlopen", fake_urlopen)
    body = {"schema_version": "mcp.wiki-approval-lookup.v1", "approval_id": str(uuid4())}
    result = BackendWikiApprovalAuthority("https://document.internal/", "token", 7).lookup(body)
    assert result == {"schema_version": "mcp.wiki-approval-status.v1", "status": "VALID", "reason": None}
    assert captured == {
        "url": "https://document.internal/internal/mcp/wiki/approvals/lookup",
        "token": "token",
        "body": body,
        "timeout": 7,
    }

    monkeypatch.setattr(apply_authority, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
    assert BackendWikiApprovalAuthority("https://document.internal", "token", 7).lookup(body) == {
        "schema_version": "mcp.wiki-approval-status.v1",
        "status": "UNKNOWN",
        "reason": "AUTHZ_UNAVAILABLE",
    }


def test_publication_receipt_uses_exact_service_authenticated_http_body(monkeypatch):
    captured = {}
    event = {
        "event_id": str(uuid4()), "event_type": "mcp.wiki.publication.v1",
        "approval_id": str(uuid4()), "operation_id": "document-operation-1",
        "draft_id": str(uuid4()), "workspace_id": "workspace-1", "user_id": "user-1",
        "grant_id": str(uuid4()), "client_id": "https://client.example/metadata",
        "kind": "LINT", "outcome": "PUBLISHED", "changed": True,
        "published_reference_hash": "sha256:" + "f" * 64,
        "committed_at": "2026-11-08T08:00:00.000Z",
    }

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b""

    def fake_urlopen(request, timeout):
        captured.update({
            "url": request.full_url,
            "token": request.get_header("X-agent-service-token"),
            "body": json.loads(request.data),
            "timeout": timeout,
        })
        return Response()

    monkeypatch.setenv("AGENT_INTERNAL_TOKEN", "token")
    monkeypatch.setenv("DOCUMENT_INTERNAL_BASE_URL", "https://document.internal/")
    monkeypatch.setenv("MCP_WIKI_PUBLICATION_RECEIPT_TIMEOUT_SECONDS", "9")
    monkeypatch.setattr(apply_repository, "urlopen", fake_urlopen)
    apply_repository._send_publication_receipt(event)
    assert captured == {
        "url": "https://document.internal/internal/mcp/wiki/publications/receipt",
        "token": "token",
        "body": event,
        "timeout": 9,
    }
