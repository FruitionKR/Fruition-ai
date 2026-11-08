from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, StrictInt, field_validator

from app.modules.mcp_wiki_draft.interfaces.http.schemas import (
    ExactModel,
    bounded_nonblank,
    canonical_uuid4,
)


_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}")
_MAX_SIGNED_LONG = 2**63 - 1


class ApplyWikiActorRequest(ExactModel):
    invocation_id: str
    grant_id: str
    user_id: str
    workspace_id: str
    client_id: str
    policy_generation: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    origin: Literal["MCP_CLIENT"]
    spec_version: Literal["2025-11-25"]
    tool_name: Literal["apply_wiki_draft"]

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
    def client(cls, value: str) -> str:
        value = bounded_nonblank(value, "client_id")
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


class ApplyWikiDraftRequest(ExactModel):
    schema_version: Literal["mcp.wiki-apply.v1"]
    actor: ApplyWikiActorRequest
    approval_id: str
    operation_id: str
    draft_id: str
    draft_version: StrictInt = Field(ge=1, le=_MAX_SIGNED_LONG)
    draft_hash: str
    public_state_hash: str
    approval_mode: Literal["EXPLICIT_WEB"]

    @field_validator("approval_id", "draft_id")
    @classmethod
    def uuid4(cls, value: str) -> str:
        return canonical_uuid4(value)

    @field_validator("operation_id")
    @classmethod
    def operation(cls, value: str) -> str:
        if not _OPERATION_ID.fullmatch(value):
            raise ValueError("operation_id has an invalid format")
        return value

    @field_validator("draft_hash", "public_state_hash")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("hash must be a lowercase sha256 digest")
        return value
