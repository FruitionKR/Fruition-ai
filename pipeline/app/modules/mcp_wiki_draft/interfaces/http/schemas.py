from __future__ import annotations

import re
from typing import Literal, Union
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_DOCUMENT_ID = re.compile(r"(?:doc_|chatdoc_)[A-Za-z0-9_-]{1,249}")
_BLOCK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}")
_MAX_SIGNED_LONG = 2**63 - 1


class ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def canonical_uuid4(value: str) -> str:
    parsed = UUID(value)
    if str(parsed) != value or parsed.version != 4:
        raise ValueError("must be a canonical UUIDv4")
    return value


def bounded_nonblank(value: str, field: str, limit: int = 2048) -> str:
    if value != value.strip() or not value or len(value) > limit:
        raise ValueError(f"{field} must be nonblank, unpadded, and bounded")
    return value


class McpWikiActorRequest(ExactModel):
    invocation_id: str
    grant_id: str
    user_id: str
    workspace_id: str
    client_id: str
    policy_generation: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    origin: Literal["MCP_CLIENT"]
    spec_version: Literal["2025-11-25"]
    tool_name: Literal[
        "start_ingest", "resume_ingest", "get_window", "submit_window",
        "finalize_ingest", "submit_lint_changes",
    ]

    @field_validator("invocation_id", "grant_id")
    @classmethod
    def uuid4(cls, value: str) -> str:
        return canonical_uuid4(value)

    @field_validator("user_id", "workspace_id")
    @classmethod
    def actor_id(cls, value: str, info) -> str:
        return bounded_nonblank(value, info.field_name, 255)

    @field_validator("client_id")
    @classmethod
    def https_client(cls, value: str) -> str:
        bounded_nonblank(value, "client_id")
        parsed = urlsplit(value)
        try:
            parsed.port
        except ValueError:
            raise ValueError("client_id has an invalid authority") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("client_id must be a clean absolute HTTPS URL")
        return value


class StartIngestRequest(ExactModel):
    schema_version: Literal["mcp.wiki-draft.v1"]
    actor: McpWikiActorRequest
    idempotency_key: str
    request_hash: str
    document_id: str

    @field_validator("idempotency_key")
    @classmethod
    def key(cls, value: str) -> str:
        return bounded_nonblank(value, "idempotency_key", 255)

    @field_validator("request_hash")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("request_hash must be a lowercase sha256 digest")
        return value

    @field_validator("document_id")
    @classmethod
    def document(cls, value: str) -> str:
        if not _DOCUMENT_ID.fullmatch(value):
            raise ValueError("document_id has an invalid format")
        return value


class ResumeIngestRequest(ExactModel):
    schema_version: Literal["mcp.wiki-draft.v1"]
    actor: McpWikiActorRequest


class ReadWindowRequest(ResumeIngestRequest):
    window_ordinal: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)


class EvidenceRequest(ExactModel):
    snapshot_id: str
    block_id: str
    quote: str

    @field_validator("snapshot_id")
    @classmethod
    def snapshot(cls, value: str) -> str:
        return canonical_uuid4(value)

    @field_validator("block_id")
    @classmethod
    def block(cls, value: str) -> str:
        if not _BLOCK_ID.fullmatch(value):
            raise ValueError("block_id has an invalid format")
        return value


class StatementRequest(ExactModel):
    statement_id: str
    text: str
    evidence: list[EvidenceRequest]


class LinkRequest(ExactModel):
    source_statement_id: str
    target_concept_name: str
    relation: Literal["concept_related_to"]


class ContributionRequest(ExactModel):
    concept_name: str
    merge_target_page_id: str | None = None
    statements: list[StatementRequest]
    links: list[LinkRequest]

    @field_validator("merge_target_page_id")
    @classmethod
    def merge_target(cls, value: str | None) -> str | None:
        if value is not None and not _BLOCK_ID.fullmatch(value):
            raise ValueError("merge_target_page_id has an invalid format")
        return value


class SubmitWindowRequest(ExactModel):
    schema_version: Literal["mcp.wiki-draft.v1"]
    actor: McpWikiActorRequest
    idempotency_key: str
    request_hash: str
    snapshot_id: str
    window_hash: str
    contributions: list[ContributionRequest]

    @field_validator("idempotency_key")
    @classmethod
    def key(cls, value: str) -> str:
        return bounded_nonblank(value, "idempotency_key", 255)

    @field_validator("request_hash", "window_hash")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("hash must be a lowercase sha256 digest")
        return value

    @field_validator("snapshot_id")
    @classmethod
    def snapshot(cls, value: str) -> str:
        return canonical_uuid4(value)


class FinalizeIngestRequest(ExactModel):
    schema_version: Literal["mcp.wiki-draft.v1"]
    actor: McpWikiActorRequest
    idempotency_key: str
    request_hash: str
    snapshot_id: str
    expected_submitted_window_count: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    # document의 크레딧 판정. request hash·멱등성에는 넣지 않아 값이 달라도 같은 결과를 재생한다.
    jev_allowed: bool = False

    @field_validator("idempotency_key")
    @classmethod
    def key(cls, value: str) -> str:
        return bounded_nonblank(value, "idempotency_key", 255)

    @field_validator("request_hash")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("request_hash must be a lowercase sha256 digest")
        return value

    @field_validator("snapshot_id")
    @classmethod
    def snapshot(cls, value: str) -> str:
        return canonical_uuid4(value)


class TargetPageSnapshotRequest(ExactModel):
    page_id: str
    version: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    content_hash: str

    @field_validator("page_id")
    @classmethod
    def page(cls, value: str) -> str:
        return bounded_nonblank(value, "page_id", 255)

    @field_validator("content_hash")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("content_hash must be a lowercase sha256 digest")
        return value


class SourceSnapshotRequest(ExactModel):
    snapshot_id: str
    document_id: str
    source_version: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    source_content_hash: str

    @field_validator("snapshot_id")
    @classmethod
    def snapshot(cls, value: str) -> str:
        return canonical_uuid4(value)

    @field_validator("document_id")
    @classmethod
    def document(cls, value: str) -> str:
        if not _DOCUMENT_ID.fullmatch(value):
            raise ValueError("document_id has an invalid format")
        return value

    @field_validator("source_content_hash")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("source_content_hash must be a lowercase sha256 digest")
        return value


class UpsertStatementArguments(ExactModel):
    statement_id: str | None
    text: str
    evidence: list[EvidenceRequest]


class RemoveStatementArguments(ExactModel):
    statement_id: str
    expected_text_hash: str

    @field_validator("expected_text_hash")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("expected_text_hash must be a lowercase sha256 digest")
        return value


class MergeConceptsArguments(ExactModel):
    source_page_ids: list[str]
    destination_page_id: str


class LinkArguments(ExactModel):
    source_ref: str
    target_ref: str
    relation: str


class DeleteOrphanPageArguments(ExactModel):
    page_id: str
    expected_content_hash: str

    @field_validator("expected_content_hash")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("expected_content_hash must be a lowercase sha256 digest")
        return value


LintArguments = Union[
    UpsertStatementArguments,
    RemoveStatementArguments,
    MergeConceptsArguments,
    LinkArguments,
    DeleteOrphanPageArguments,
]


class LintOperationRequest(ExactModel):
    operation_id: str
    kind: Literal[
        "UPSERT_STATEMENT", "REMOVE_STATEMENT", "MERGE_CONCEPTS",
        "ADD_LINK", "REMOVE_LINK", "DELETE_ORPHAN_PAGE",
    ]
    target_page_id: str
    arguments: LintArguments
    depends_on: list[str]

    @field_validator("operation_id", "target_page_id")
    @classmethod
    def identifier(cls, value: str, info) -> str:
        return bounded_nonblank(value, info.field_name, 255)

    @model_validator(mode="after")
    def exact_arguments(self):
        expected = {
            "UPSERT_STATEMENT": UpsertStatementArguments,
            "REMOVE_STATEMENT": RemoveStatementArguments,
            "MERGE_CONCEPTS": MergeConceptsArguments,
            "ADD_LINK": LinkArguments,
            "REMOVE_LINK": LinkArguments,
            "DELETE_ORPHAN_PAGE": DeleteOrphanPageArguments,
        }[self.kind]
        if not isinstance(self.arguments, expected):
            raise ValueError("arguments do not match lint operation kind")
        return self


class SubmitLintDraftRequest(ExactModel):
    schema_version: Literal["mcp.wiki-draft.v1"]
    actor: McpWikiActorRequest
    idempotency_key: str
    request_hash: str
    target_snapshot: list[TargetPageSnapshotRequest]
    source_snapshots: list[SourceSnapshotRequest]
    operations: list[LintOperationRequest]
    # document의 크레딧 판정. request hash·멱등성에는 넣지 않아 값이 달라도 같은 결과를 재생한다.
    jev_allowed: bool = False

    @field_validator("idempotency_key")
    @classmethod
    def key(cls, value: str) -> str:
        return bounded_nonblank(value, "idempotency_key", 255)

    @field_validator("request_hash")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("request_hash must be a lowercase sha256 digest")
        return value
