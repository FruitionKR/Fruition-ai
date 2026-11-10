from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from app.modules.mcp_read.application.wiki_search import WikiSearchActor, WikiSearchRequest


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class McpWikiSearchActorRequest(_ExactModel):
    invocation_id: str
    grant_id: str
    user_id: str
    workspace_id: str
    client_id: str
    policy_generation: StrictInt = Field(ge=1)
    origin: Literal["MCP_CLIENT"]
    spec_version: Literal["2025-11-25"]
    tool_name: Literal["search_wiki"]

    @field_validator("invocation_id", "grant_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        parsed = UUID(value)
        if str(parsed) != value or parsed.version != 4:
            raise ValueError("must be a canonical UUIDv4")
        return value

    @field_validator("user_id", "workspace_id", "client_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("client_id")
    @classmethod
    def absolute_client_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            parsed.port
        except ValueError:
            raise ValueError("client_id must contain a valid HTTPS authority") from None
        if (
            value != value.strip()
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or parsed.scheme != "https"
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("client_id must be a clean absolute HTTPS URL without credentials or fragment")
        return value

    def to_application(self) -> WikiSearchActor:
        return WikiSearchActor(**self.model_dump())


class McpWikiSearchRequest(_ExactModel):
    schema_version: Literal["mcp.wiki-search.v1"]
    actor: McpWikiSearchActorRequest
    query: str
    cursor: str | None
    page_size: StrictInt
    jev_allowed: bool = False

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value

    def to_application(self) -> WikiSearchRequest:
        return WikiSearchRequest(
            actor=self.actor.to_application(),
            query=self.query,
            cursor=self.cursor,
            page_size=self.page_size,
            jev_allowed=self.jev_allowed,
        )


class McpWikiEvidenceResponse(_ExactModel):
    document_id: str
    block_id: str


class McpWikiSearchResultResponse(_ExactModel):
    page_id: str
    page_type: Literal["SOURCE", "CONCEPT"]
    title: str
    version: StrictInt = Field(ge=1)
    content_hash: str
    snippet: str
    evidence: list[McpWikiEvidenceResponse]


class McpWikiSearchResponse(_ExactModel):
    schema_version: Literal["mcp.wiki-search.v1"]
    results: list[McpWikiSearchResultResponse]
    next_cursor: str | None
    truncated: bool
    data_boundary: Literal["UNTRUSTED_WORKSPACE_CONTENT"]
