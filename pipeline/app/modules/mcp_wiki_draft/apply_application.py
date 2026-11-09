from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.modules.mcp_wiki_draft.application import McpWikiDraftError, canonical_hash
from app.modules.mcp_wiki_draft.interfaces.http.apply_schemas import ApplyWikiDraftRequest


@dataclass(frozen=True)
class ApplyClaim:
    run_id: str
    lookup: dict[str, Any]
    stored_result: dict[str, Any] | None = None


class ApplyRepository(Protocol):
    def execution_lock(self, approval_id: str, operation_id: str): ...
    def claim(self, payload: ApplyWikiDraftRequest, request_hash: str) -> ApplyClaim: ...
    def publish(self, claim: ApplyClaim, payload: ApplyWikiDraftRequest) -> dict[str, Any]: ...
    def finish_without_publication(
        self, claim: ApplyClaim, payload: ApplyWikiDraftRequest, state: str, error_code: str
    ) -> dict[str, Any]: ...
    def result(
        self, approval_id: str, operation_id: str, workspace_id: str, user_id: str
    ) -> dict[str, Any]: ...


class ApprovalAuthority(Protocol):
    def lookup(self, request: dict[str, Any]) -> dict[str, str | None]: ...


class McpWikiDraftApply:
    def __init__(self, repository: ApplyRepository, authority: ApprovalAuthority) -> None:
        self._repository = repository
        self._authority = authority

    def apply(self, draft_id: str, payload: ApplyWikiDraftRequest) -> dict[str, Any]:
        if payload.draft_id != draft_id:
            raise McpWikiDraftError(409, "IDEMPOTENCY_CONFLICT", "Path and body draft identity differ.")
        request_hash = canonical_hash(_canonical_apply(payload))
        with self._repository.execution_lock(payload.approval_id, payload.operation_id):
            claim = self._repository.claim(payload, request_hash)
            if claim.stored_result is not None:
                return claim.stored_result
            authority = self._authority.lookup(claim.lookup)
            status = authority["status"]
            reason = authority.get("reason")
            if status == "VALID":
                return self._repository.publish(claim, payload)
            if status == "UNKNOWN":
                return self._repository.finish_without_publication(
                    claim, payload, "UNKNOWN", reason or "AUTHZ_UNAVAILABLE"
                )
            if status == "FENCED":
                return self._repository.finish_without_publication(
                    claim, payload, "HALTED", reason or "PERMISSION_REVOKED"
                )
            return self._repository.finish_without_publication(
                claim,
                payload,
                "CONFLICTED",
                reason or ("APPROVAL_NOT_FOUND" if status == "NOT_FOUND" else "APPROVAL_INVALID"),
            )

    def result(
        self, approval_id: str, operation_id: str, workspace_id: str, user_id: str
    ) -> dict[str, Any]:
        for value, field in ((workspace_id, "workspace"), (user_id, "user")):
            if value != value.strip() or not value or len(value) > 255:
                raise McpWikiDraftError(400, "INVALID_INPUT", f"{field} owner is invalid.")
        return self._repository.result(approval_id, operation_id, workspace_id, user_id)


def _canonical_apply(payload: ApplyWikiDraftRequest) -> dict[str, Any]:
    actor = payload.actor.model_dump()
    actor.pop("invocation_id")
    return {
        "schema_version": payload.schema_version,
        "actor": actor,
        "approval_id": payload.approval_id,
        "operation_id": payload.operation_id,
        "draft_id": payload.draft_id,
        "draft_version": payload.draft_version,
        "draft_hash": payload.draft_hash,
        "public_state_hash": payload.public_state_hash,
        "approval_mode": payload.approval_mode,
    }
