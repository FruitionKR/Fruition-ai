from typing import Literal

from pydantic import BaseModel, Field

from app.modules.wiki_schema.domain.entities import SchemaFilterResult, SchemaFragments, SchemaIssue, WikiSchemaRecord


class WikiSchemaPreviewRequest(BaseModel):
    raw_markdown: str = Field(..., min_length=1)


class SchemaFragmentsResponse(BaseModel):
    global_markdown: str
    query_markdown: str
    ingest_markdown: str
    edit_markdown: str
    concept_markdown: str
    template_markdown: str

    @classmethod
    def from_domain(cls, fragments: SchemaFragments) -> "SchemaFragmentsResponse":
        return cls(
            global_markdown=fragments.global_markdown,
            query_markdown=fragments.query_markdown,
            ingest_markdown=fragments.ingest_markdown,
            edit_markdown=fragments.edit_markdown,
            concept_markdown=fragments.concept_markdown,
            template_markdown=fragments.template_markdown,
        )


class SchemaIssueResponse(BaseModel):
    severity: Literal["blocked", "unclear"]
    category: str
    text: str
    reason: str
    section: str | None = None

    @classmethod
    def from_domain(cls, issue: SchemaIssue) -> "SchemaIssueResponse":
        return cls(
            severity=issue.severity,
            category=issue.category,
            text=issue.text,
            reason=issue.reason,
            section=issue.section,
        )


class WikiSchemaPreviewResponse(BaseModel):
    fragments: SchemaFragmentsResponse
    issues: list[SchemaIssueResponse]
    preview_markdown: str
    has_blocked_issues: bool

    @classmethod
    def from_domain(cls, result: SchemaFilterResult, preview_markdown: str) -> "WikiSchemaPreviewResponse":
        return cls(
            fragments=SchemaFragmentsResponse.from_domain(result.fragments),
            issues=[SchemaIssueResponse.from_domain(issue) for issue in result.issues],
            preview_markdown=preview_markdown,
            has_blocked_issues=bool(result.blocked_issues),
        )


class CreateWikiSchemaDraftRequest(BaseModel):
    raw_markdown: str = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    name: str = Field(default="default", min_length=1)


class WikiSchemaResponse(BaseModel):
    id: str
    workspace_id: str
    user_id: str
    name: str
    raw_markdown: str
    fragments: SchemaFragmentsResponse
    issues: list[SchemaIssueResponse]
    preview_markdown: str
    has_blocked_issues: bool
    status: str
    schema_version: str
    created_at: str | None = None
    updated_at: str | None = None
    activated_at: str | None = None

    @classmethod
    def from_domain(cls, record: WikiSchemaRecord) -> "WikiSchemaResponse":
        return cls(
            id=record.id,
            workspace_id=record.workspace_id,
            user_id=record.user_id,
            name=record.name,
            raw_markdown=record.raw_markdown,
            fragments=SchemaFragmentsResponse.from_domain(record.fragments),
            issues=[SchemaIssueResponse.from_domain(issue) for issue in record.issues],
            preview_markdown=record.preview_markdown,
            has_blocked_issues=bool([issue for issue in record.issues if issue.severity == "blocked"]),
            status=record.status,
            schema_version=record.schema_version,
            created_at=record.created_at.isoformat() if record.created_at else None,
            updated_at=record.updated_at.isoformat() if record.updated_at else None,
            activated_at=record.activated_at.isoformat() if record.activated_at else None,
        )


class CreateWikiSchemaDraftResponse(BaseModel):
    wiki_schema: WikiSchemaResponse
