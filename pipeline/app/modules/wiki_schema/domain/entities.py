from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


SchemaFeature = Literal["query", "ingest", "edit", "concept", "template"]
SchemaStatus = Literal["draft", "active", "rejected"]
SchemaIssueSeverity = Literal["blocked", "unclear"]
SchemaIssueCategory = Literal[
    "instruction_override",
    "hidden_prompt",
    "policy_weakening",
    "permission_escalation",
    "secret",
    "role_override",
    "organizer_blocked",
    "unclear_preference",
]


@dataclass(frozen=True)
class SchemaFragments:
    global_markdown: str = ""
    query_markdown: str = ""
    ingest_markdown: str = ""
    edit_markdown: str = ""
    concept_markdown: str = ""
    template_markdown: str = ""


@dataclass(frozen=True)
class SchemaIssue:
    severity: SchemaIssueSeverity
    category: SchemaIssueCategory
    text: str
    reason: str
    section: str | None = None


@dataclass(frozen=True)
class SchemaFilterResult:
    fragments: SchemaFragments
    issues: list[SchemaIssue] = field(default_factory=list)

    @property
    def blocked_issues(self) -> list[SchemaIssue]:
        return [issue for issue in self.issues if issue.severity == "blocked"]


@dataclass(frozen=True)
class SchemaOrganizerCandidate:
    fragments: SchemaFragments
    blocked_candidates: list[str] = field(default_factory=list)
    unclear_items: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WikiSchemaRecord:
    id: str
    workspace_id: str
    user_id: str
    name: str
    raw_markdown: str
    fragments: SchemaFragments
    preview_markdown: str
    issues: list[SchemaIssue]
    status: SchemaStatus
    schema_version: str = "1.0"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    activated_at: datetime | None = None
