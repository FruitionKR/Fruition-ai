from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Any, Protocol

from app.core.ai_markdown_sanitizer import external_urls, sanitize_ai_markdown
from app.modules.mcp_wiki_draft.interfaces.http.schemas import (
    FinalizeIngestRequest,
    McpWikiActorRequest,
    StartIngestRequest,
    SubmitLintDraftRequest,
    SubmitWindowRequest,
)


class McpWikiDraftError(ValueError):
    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class IntakeLimits:
    source_response_bytes: int
    source_blocks: int
    source_text_bytes: int
    window_blocks: int
    window_text_bytes: int
    active_per_user: int
    active_per_workspace: int
    active_storage_bytes: int
    run_ttl_seconds: int


@dataclass(frozen=True)
class SubmissionPolicy:
    max_contributions: int
    max_statements: int
    max_evidence: int
    max_links: int
    max_name_chars: int
    max_statement_chars: int
    max_quote_chars: int
    lexical_overlap: float
    instruction_patterns: tuple[re.Pattern[str], ...]
    policy_version: str
    evalset_id: str


@dataclass(frozen=True)
class PreviewLimits:
    max_items: int
    max_bytes: int
    max_source_bytes: int


@dataclass(frozen=True)
class LintLimits:
    max_operations: int
    max_target_pages: int
    max_source_snapshots: int
    max_total_source_bytes: int
    max_structural_changes: int
    max_deletes: int
    draft_ttl_seconds: int
    relations: frozenset[str]
    policy_version: str


_INT_CONFIG = {
    "source_response_bytes": "MCP_WIKI_DRAFT_SOURCE_RESPONSE_MAX_BYTES",
    "source_blocks": "MCP_WIKI_DRAFT_SOURCE_MAX_BLOCKS",
    "source_text_bytes": "MCP_WIKI_DRAFT_SOURCE_MAX_TEXT_BYTES",
    "window_blocks": "MCP_WIKI_DRAFT_WINDOW_MAX_BLOCKS",
    "window_text_bytes": "MCP_WIKI_DRAFT_WINDOW_MAX_TEXT_BYTES",
    "active_per_user": "MCP_WIKI_DRAFT_MAX_ACTIVE_PER_USER",
    "active_per_workspace": "MCP_WIKI_DRAFT_MAX_ACTIVE_PER_WORKSPACE",
    "active_storage_bytes": "MCP_WIKI_DRAFT_MAX_ACTIVE_STORAGE_BYTES",
    "run_ttl_seconds": "MCP_WIKI_DRAFT_RUN_TTL_SECONDS",
}
_SUBMISSION_INT_CONFIG = {
    "max_contributions": "MCP_WIKI_DRAFT_MAX_CONTRIBUTIONS",
    "max_statements": "MCP_WIKI_DRAFT_MAX_STATEMENTS",
    "max_evidence": "MCP_WIKI_DRAFT_MAX_EVIDENCE_PER_STATEMENT",
    "max_links": "MCP_WIKI_DRAFT_MAX_LINKS",
    "max_name_chars": "MCP_WIKI_DRAFT_MAX_NAME_CHARS",
    "max_statement_chars": "MCP_WIKI_DRAFT_MAX_STATEMENT_CHARS",
    "max_quote_chars": "MCP_WIKI_DRAFT_MAX_QUOTE_CHARS",
}
_PREVIEW_INT_CONFIG = {
    "max_items": "MCP_WIKI_DRAFT_PREVIEW_MAX_ITEMS",
    "max_bytes": "MCP_WIKI_DRAFT_PREVIEW_MAX_BYTES",
    "max_source_bytes": "MCP_WIKI_DRAFT_PREVIEW_MAX_SOURCE_BYTES",
}
_LINT_INT_CONFIG = {
    "max_operations": "MCP_WIKI_DRAFT_LINT_MAX_OPERATIONS",
    "max_target_pages": "MCP_WIKI_DRAFT_LINT_MAX_TARGET_PAGES",
    "max_source_snapshots": "MCP_WIKI_DRAFT_LINT_MAX_SOURCE_SNAPSHOTS",
    "max_total_source_bytes": "MCP_WIKI_DRAFT_LINT_MAX_TOTAL_SOURCE_BYTES",
    "max_structural_changes": "MCP_WIKI_DRAFT_LINT_MAX_STRUCTURAL_CHANGES",
    "max_deletes": "MCP_WIKI_DRAFT_LINT_MAX_DELETES",
    "draft_ttl_seconds": "MCP_WIKI_DRAFT_LINT_TTL_SECONDS",
}


def _positive_config(names: dict[str, str], unavailable: str) -> dict[str, int]:
    if any(not os.environ.get(name, "").strip() for name in names.values()):
        raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", unavailable, retryable=True)
    try:
        values = {field: int(os.environ[name]) for field, name in names.items()}
    except ValueError:
        raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", unavailable, retryable=True) from None
    if any(value <= 0 for value in values.values()):
        raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", unavailable, retryable=True)
    return values


def load_intake_limits() -> IntakeLimits:
    values = _positive_config(_INT_CONFIG, "MCP wiki draft intake limits are not configured.")
    if values["source_text_bytes"] > values["source_response_bytes"]:
        raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki draft intake limits are invalid.")
    return IntakeLimits(**values)


def load_submission_policy() -> SubmissionPolicy:
    values = _positive_config(_SUBMISSION_INT_CONFIG, "MCP wiki submission policy is not configured.")
    required = (
        "MCP_WIKI_DRAFT_LEXICAL_OVERLAP_THRESHOLD",
        "MCP_WIKI_DRAFT_INSTRUCTION_PATTERNS_JSON",
        "MCP_WIKI_DRAFT_VALIDATION_POLICY_VERSION",
        "MCP_WIKI_DRAFT_VALIDATION_EVALSET_ID",
    )
    if any(not os.environ.get(name, "").strip() for name in required):
        raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki submission policy is not configured.")
    try:
        threshold = float(os.environ[required[0]])
        raw_patterns = json.loads(os.environ[required[1]])
        if not 0 <= threshold <= 1 or not isinstance(raw_patterns, list) or not raw_patterns:
            raise ValueError
        patterns = tuple(re.compile(item) for item in raw_patterns if isinstance(item, str) and item)
        if len(patterns) != len(raw_patterns):
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError, re.error):
        raise McpWikiDraftError(503, "DEPENDENCY_UNAVAILABLE", "MCP wiki submission policy is invalid.") from None
    return SubmissionPolicy(
        **values,
        lexical_overlap=threshold,
        instruction_patterns=patterns,
        policy_version=os.environ[required[2]].strip(),
        evalset_id=os.environ[required[3]].strip(),
    )


def load_preview_limits() -> PreviewLimits:
    return PreviewLimits(**_positive_config(
        _PREVIEW_INT_CONFIG, "MCP wiki preview limits are not configured."
    ))


def load_lint_limits() -> LintLimits:
    values = _positive_config(_LINT_INT_CONFIG, "MCP wiki lint limits are not configured.")
    relation_config = os.environ.get("MCP_WIKI_DRAFT_LINT_RELATIONS_JSON", "").strip()
    policy_version = os.environ.get("MCP_WIKI_DRAFT_LINT_POLICY_VERSION", "").strip()
    try:
        relations = json.loads(relation_config)
        if not policy_version or not isinstance(relations, list) or not relations or any(
            not isinstance(item, str) or not item or item != item.strip() for item in relations
        ):
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError):
        raise McpWikiDraftError(
            503, "DEPENDENCY_UNAVAILABLE", "MCP wiki lint policy is not configured.", retryable=True
        ) from None
    return LintLimits(**values, relations=frozenset(relations), policy_version=policy_version)


def canonical_hash(value: object) -> str:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise McpWikiDraftError(400, "INVALID_INPUT", "Request is not canonical JSON.") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def actor_identity(actor: McpWikiActorRequest) -> dict[str, object]:
    value = actor.model_dump()
    value.pop("invocation_id")
    value.pop("tool_name")
    return value


def request_actor(actor: McpWikiActorRequest) -> dict[str, object]:
    value = actor.model_dump()
    value.pop("invocation_id")
    return value


class SourceReader(Protocol):
    def read(self, actor: McpWikiActorRequest, document_id: str, limits: IntakeLimits) -> dict[str, Any]: ...


class IntakeRepository(Protocol):
    def find_start(self, payload: StartIngestRequest, request_hash: str) -> dict[str, Any] | None: ...
    def start(self, payload: StartIngestRequest, source: dict[str, Any], limits: IntakeLimits, request_hash: str) -> dict[str, Any]: ...
    def resume(self, run_id: str, actor: McpWikiActorRequest, *, window_id: str | None = None) -> dict[str, Any]: ...
    def read_window(self, run_id: str, actor: McpWikiActorRequest, ordinal: int, limits: IntakeLimits) -> dict[str, Any]: ...
    def submit(self, run_id: str, window_id: str, payload: SubmitWindowRequest, normalized: list[dict[str, Any]], accepted_hash: str, request_hash: str) -> dict[str, Any]: ...
    def finalize(self, run_id: str, payload: FinalizeIngestRequest, limits: PreviewLimits) -> dict[str, Any]: ...
    def find_lint(self, payload: SubmitLintDraftRequest, request_hash: str) -> dict[str, Any] | None: ...
    def create_lint(self, payload: SubmitLintDraftRequest, normalized: list[dict[str, Any]], sources: list[dict[str, Any]], intake_limits: IntakeLimits, lint_limits: LintLimits, preview_limits: PreviewLimits, request_hash: str) -> dict[str, Any]: ...
    def preview(self, draft_id: str, draft_version: int, workspace_id: str, user_id: str) -> dict[str, Any]: ...


class McpWikiDraftIntake:
    def __init__(self, repository: IntakeRepository, source_reader: SourceReader) -> None:
        self._repository = repository
        self._source_reader = source_reader

    def start(self, payload: StartIngestRequest) -> dict[str, Any]:
        if payload.actor.tool_name != "start_ingest":
            raise McpWikiDraftError(400, "INVALID_INPUT", "actor.tool_name does not match the endpoint.")
        expected = canonical_hash({
            "schema_version": payload.schema_version,
            "actor": request_actor(payload.actor),
            "document_id": payload.document_id,
        })
        if payload.request_hash != expected:
            raise McpWikiDraftError(400, "INVALID_INPUT", "request_hash does not match the command.")
        replay = self._repository.find_start(payload, expected)
        if replay is not None:
            return replay
        limits = load_intake_limits()
        source = self._source_reader.read(payload.actor, payload.document_id, limits)
        return self._repository.start(payload, source, limits, expected)

    def resume(self, run_id: str, actor: McpWikiActorRequest) -> dict[str, Any]:
        if actor.tool_name != "resume_ingest":
            raise McpWikiDraftError(400, "INVALID_INPUT", "actor.tool_name does not match the endpoint.")
        return self._repository.resume(run_id, actor)

    def read_window(self, run_id: str, actor: McpWikiActorRequest, ordinal: int) -> dict[str, Any]:
        if actor.tool_name != "get_window":
            raise McpWikiDraftError(400, "INVALID_INPUT", "actor.tool_name does not match the endpoint.")
        return self._repository.read_window(run_id, actor, ordinal, load_intake_limits())

    def submit(self, run_id: str, window_id: str, payload: SubmitWindowRequest) -> dict[str, Any]:
        if payload.actor.tool_name != "submit_window":
            raise McpWikiDraftError(400, "INVALID_INPUT", "actor.tool_name does not match the endpoint.")
        policy = load_submission_policy()
        context = self._repository.resume(run_id, payload.actor, window_id=window_id)
        source_mapping = [{
            "snapshot_id": context["snapshot_id"],
            "document_id": context["document_id"],
            "source_version": context["source_version"],
            "source_content_hash": context["source_content_hash"],
        }]
        normalized = _validate_contributions(payload, context, policy)
        command = {
            "schema_version": payload.schema_version,
            "actor": request_actor(payload.actor),
            "run_id": run_id,
            "window_id": window_id,
            "snapshot_id": payload.snapshot_id,
            "window_hash": payload.window_hash,
            "source_snapshots": source_mapping,
            "contributions": normalized,
        }
        expected = canonical_hash(command)
        if payload.request_hash != expected:
            raise McpWikiDraftError(400, "INVALID_INPUT", "request_hash does not match the command.")
        accepted_hash = canonical_hash({
            "run_id": run_id,
            "window_id": window_id,
            "source_snapshots": source_mapping,
            "contributions": normalized,
            "validation_policy_version": policy.policy_version,
            "validation_evalset_id": policy.evalset_id,
        })
        return self._repository.submit(
            run_id, window_id, payload, normalized, accepted_hash, expected
        )

    def finalize(self, run_id: str, payload: FinalizeIngestRequest) -> dict[str, Any]:
        if payload.actor.tool_name != "finalize_ingest":
            raise McpWikiDraftError(400, "INVALID_INPUT", "actor.tool_name does not match the endpoint.")
        return self._repository.finalize(run_id, payload, load_preview_limits())

    def lint(self, payload: SubmitLintDraftRequest) -> dict[str, Any]:
        if payload.actor.tool_name != "submit_lint_changes":
            raise McpWikiDraftError(400, "INVALID_INPUT", "actor.tool_name does not match the endpoint.")
        command = {
            "schema_version": payload.schema_version,
            "actor": request_actor(payload.actor),
            "target_snapshot": sorted(
                (item.model_dump() for item in payload.target_snapshot), key=lambda item: item["page_id"]
            ),
            "source_snapshots": sorted(
                (item.model_dump() for item in payload.source_snapshots), key=lambda item: item["snapshot_id"]
            ),
            "operations": [item.model_dump() for item in payload.operations],
        }
        expected = canonical_hash(command)
        if payload.request_hash != expected:
            raise McpWikiDraftError(400, "INVALID_INPUT", "request_hash does not match the command.")
        replay = self._repository.find_lint(payload, expected)
        if replay is not None:
            return replay
        intake_limits = load_intake_limits()
        lint_limits = load_lint_limits()
        policy = load_submission_policy()
        normalized = _validate_lint_operations(payload, lint_limits, policy)
        if not 1 <= len(payload.source_snapshots) <= lint_limits.max_source_snapshots:
            raise McpWikiDraftError(400, "INVALID_INPUT", "source snapshot count is outside the configured bound.")
        sources: list[dict[str, Any]] = []
        total_source_bytes = 0
        for mapping in sorted(payload.source_snapshots, key=lambda item: item.snapshot_id):
            source = self._source_reader.read(payload.actor, mapping.document_id, intake_limits)
            if (
                int(source["source_version"]) != mapping.source_version
                or str(source["source_content_hash"]) != mapping.source_content_hash
            ):
                raise McpWikiDraftError(409, "SOURCE_CHANGED", "Authoritative source changed during lint intake.")
            blocks = [dict(item) for item in source["blocks"]]
            total_source_bytes += sum(len(str(item["text"]).encode("utf-8")) for item in blocks)
            sources.append({**mapping.model_dump(), "blocks": blocks})
        if total_source_bytes > lint_limits.max_total_source_bytes:
            raise McpWikiDraftError(413, "SOURCE_LIMIT_EXCEEDED", "Lint sources exceed the configured total bound.")
        _validate_lint_evidence(normalized, sources, policy)
        return self._repository.create_lint(
            payload, normalized, sources, intake_limits, lint_limits, load_preview_limits(), expected
        )

    def preview(
        self, draft_id: str, draft_version: int, workspace_id: str, user_id: str
    ) -> dict[str, Any]:
        if draft_version <= 0:
            raise McpWikiDraftError(400, "INVALID_INPUT", "draft_version must be positive.")
        for value, field in ((workspace_id, "workspace"), (user_id, "user")):
            if value != value.strip() or not value or len(value) > 255:
                raise McpWikiDraftError(400, "INVALID_INPUT", f"{field} owner is invalid.")
        return self._repository.preview(draft_id, draft_version, workspace_id, user_id)


def _validate_contributions(
    payload: SubmitWindowRequest,
    context: dict[str, Any],
    policy: SubmissionPolicy,
) -> list[dict[str, Any]]:
    if not 1 <= len(payload.contributions) <= policy.max_contributions:
        raise McpWikiDraftError(400, "INVALID_INPUT", "contributions count is outside the configured bound.")
    blocks = {item["block_id"]: item["text"] for item in context["window_blocks"]}
    concepts: set[str] = set()
    statement_ids: set[str] = set()
    statement_count = evidence_count = link_count = 0
    normalized: list[dict[str, Any]] = []
    for contribution in payload.contributions:
        name = _plain_text(contribution.concept_name, "concept_name", policy.max_name_chars, policy)
        if name.casefold() in concepts:
            raise McpWikiDraftError(400, "INVALID_INPUT", "concept_name values must be unique.")
        concepts.add(name.casefold())
        local_ids: set[str] = set()
        statements: list[dict[str, Any]] = []
        if not contribution.statements:
            raise McpWikiDraftError(400, "INVALID_INPUT", "Each contribution requires at least one statement.")
        for statement in contribution.statements:
            statement_count += 1
            if statement_count > policy.max_statements:
                raise McpWikiDraftError(400, "INVALID_INPUT", "statement count exceeds the configured bound.")
            statement_id = _bounded_id(statement.statement_id, "statement_id")
            if statement_id in statement_ids:
                raise McpWikiDraftError(400, "INVALID_INPUT", "statement_id values must be unique.")
            statement_ids.add(statement_id)
            local_ids.add(statement_id)
            text = _plain_text(statement.text, "statement text", policy.max_statement_chars, policy)
            if not 1 <= len(statement.evidence) <= policy.max_evidence:
                raise McpWikiDraftError(400, "INVALID_INPUT", "evidence count is outside the configured bound.")
            evidence: list[dict[str, str]] = []
            locators: set[tuple[str, str]] = set()
            quoted_tokens: set[str] = set()
            for item in statement.evidence:
                evidence_count += 1
                if item.snapshot_id != context["snapshot_id"] or item.block_id not in blocks:
                    raise McpWikiDraftError(400, "INVALID_EVIDENCE", "Evidence is outside this immutable window.")
                if (item.snapshot_id, item.block_id) in locators:
                    raise McpWikiDraftError(400, "INVALID_EVIDENCE", "Evidence locators must be unique.")
                locators.add((item.snapshot_id, item.block_id))
                quote = _plain_text(item.quote, "quote", policy.max_quote_chars, policy)
                if quote not in blocks[item.block_id]:
                    raise McpWikiDraftError(400, "INVALID_EVIDENCE", "quote is not an exact source substring.")
                quoted_tokens.update(_tokens(quote))
                evidence.append({"snapshot_id": item.snapshot_id, "block_id": item.block_id, "quote": quote})
            tokens = _tokens(text)
            overlap = len(tokens & quoted_tokens) / len(tokens) if tokens else 0
            if overlap < policy.lexical_overlap:
                raise McpWikiDraftError(400, "LEXICAL_OVERLAP_REJECTED", "Statement does not meet the configured lexical overlap policy.")
            statements.append({"statement_id": statement_id, "text": text, "evidence": evidence})
        links: list[dict[str, str]] = []
        seen_links: set[tuple[str, str, str]] = set()
        for link in contribution.links:
            link_count += 1
            if link_count > policy.max_links:
                raise McpWikiDraftError(400, "INVALID_INPUT", "link count exceeds the configured bound.")
            source_id = _bounded_id(link.source_statement_id, "source_statement_id")
            if source_id not in local_ids:
                raise McpWikiDraftError(400, "INVALID_INPUT", "link source must be a statement in the same contribution.")
            target = _plain_text(link.target_concept_name, "target_concept_name", policy.max_name_chars, policy)
            identity = (source_id, target.casefold(), link.relation)
            if identity in seen_links:
                raise McpWikiDraftError(400, "INVALID_INPUT", "links must be unique.")
            seen_links.add(identity)
            links.append({"source_statement_id": source_id, "target_concept_name": target, "relation": link.relation})
        normalized.append({"concept_name": name, "statements": statements, "links": links})
    return normalized


def _validate_lint_operations(
    payload: SubmitLintDraftRequest,
    limits: LintLimits,
    policy: SubmissionPolicy,
) -> list[dict[str, Any]]:
    if not 1 <= len(payload.target_snapshot) <= limits.max_target_pages:
        raise McpWikiDraftError(400, "INVALID_INPUT", "target snapshot count is outside the configured bound.")
    if not 1 <= len(payload.operations) <= limits.max_operations:
        raise McpWikiDraftError(400, "INVALID_INPUT", "lint operation count is outside the configured bound.")
    target_ids = [item.page_id for item in payload.target_snapshot]
    if len(target_ids) != len(set(target_ids)):
        raise McpWikiDraftError(400, "INVALID_INPUT", "target page identities must be unique.")
    snapshot_ids = [item.snapshot_id for item in payload.source_snapshots]
    document_ids = [item.document_id for item in payload.source_snapshots]
    if len(snapshot_ids) != len(set(snapshot_ids)) or len(document_ids) != len(set(document_ids)):
        raise McpWikiDraftError(400, "INVALID_INPUT", "source snapshot mappings must be unique.")

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    structural = deletes = 0
    for operation in payload.operations:
        operation_id = _bounded_id(operation.operation_id, "operation_id")
        target_page_id = _bounded_id(operation.target_page_id, "target_page_id")
        if operation_id in seen:
            raise McpWikiDraftError(400, "INVALID_INPUT", "operation_id values must be unique.")
        if target_page_id not in target_ids:
            raise McpWikiDraftError(400, "INVALID_INPUT", "operation target is outside target_snapshot.")
        dependencies = [_bounded_id(item, "depends_on") for item in operation.depends_on]
        if len(dependencies) != len(set(dependencies)) or any(item not in seen for item in dependencies):
            raise McpWikiDraftError(400, "INVALID_INPUT", "depends_on must contain unique earlier operations.")
        arguments = operation.arguments.model_dump()
        if operation.kind == "UPSERT_STATEMENT":
            if arguments["statement_id"] is not None:
                arguments["statement_id"] = _bounded_id(arguments["statement_id"], "statement_id")
            arguments["text"] = _plain_text(
                arguments["text"], "statement text", policy.max_statement_chars, policy
            )
            if not 1 <= len(arguments["evidence"]) <= policy.max_evidence:
                raise McpWikiDraftError(400, "INVALID_INPUT", "evidence count is outside the configured bound.")
        elif operation.kind == "REMOVE_STATEMENT":
            arguments["statement_id"] = _bounded_id(arguments["statement_id"], "statement_id")
        elif operation.kind == "MERGE_CONCEPTS":
            sources = [_bounded_id(item, "source_page_id") for item in arguments["source_page_ids"]]
            destination = _bounded_id(arguments["destination_page_id"], "destination_page_id")
            if (
                not sources or len(sources) != len(set(sources)) or destination in sources
                or operation.target_page_id != destination
                or any(item not in target_ids for item in [destination, *sources])
            ):
                raise McpWikiDraftError(400, "INVALID_INPUT", "merge identities are invalid or outside target_snapshot.")
            arguments.update(source_page_ids=sources, destination_page_id=destination)
            structural += len(sources) + 1
            deletes += len(sources)
        elif operation.kind in {"ADD_LINK", "REMOVE_LINK"}:
            for field in ("source_ref", "target_ref"):
                value = arguments[field]
                if value != value.strip() or len(value) > 511 or not re.fullmatch(r"concept:[a-z0-9]+(?:-[a-z0-9]+)*", value):
                    raise McpWikiDraftError(400, "INVALID_INPUT", f"{field} is not a canonical concept reference.")
            if arguments["source_ref"] == arguments["target_ref"] or arguments["relation"] not in limits.relations:
                raise McpWikiDraftError(400, "INVALID_INPUT", "link identity or relation is not allowed.")
            structural += 1
        else:
            page_id = _bounded_id(arguments["page_id"], "page_id")
            if page_id != target_page_id:
                raise McpWikiDraftError(400, "INVALID_INPUT", "delete page identity does not match its target.")
            deletes += 1
            structural += 1
        seen.add(operation_id)
        normalized.append({
            "operation_id": operation_id,
            "kind": operation.kind,
            "target_page_id": target_page_id,
            "arguments": arguments,
            "depends_on": dependencies,
        })
    if structural > limits.max_structural_changes or deletes > limits.max_deletes:
        raise McpWikiDraftError(413, "PREVIEW_TOO_LARGE", "Lint structural changes exceed configured bounds.")
    return normalized


def _validate_lint_evidence(
    operations: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    policy: SubmissionPolicy,
) -> None:
    blocks = {
        (str(source["snapshot_id"]), str(block["block_id"])): str(block["text"])
        for source in sources
        for block in source["blocks"]
    }
    for operation in operations:
        if operation["kind"] != "UPSERT_STATEMENT":
            continue
        evidence = operation["arguments"]["evidence"]
        locators: set[tuple[str, str]] = set()
        quoted_tokens: set[str] = set()
        for item in evidence:
            locator = (str(item["snapshot_id"]), str(item["block_id"]))
            if locator in locators or locator not in blocks:
                raise McpWikiDraftError(400, "INVALID_EVIDENCE", "Evidence is duplicated or outside bound snapshots.")
            locators.add(locator)
            quote = _plain_text(str(item["quote"]), "quote", policy.max_quote_chars, policy)
            if quote not in blocks[locator]:
                raise McpWikiDraftError(400, "INVALID_EVIDENCE", "quote is not an exact source substring.")
            item["quote"] = quote
            quoted_tokens.update(_tokens(quote))
        tokens = _tokens(operation["arguments"]["text"])
        overlap = len(tokens & quoted_tokens) / len(tokens) if tokens else 0
        if overlap < policy.lexical_overlap:
            raise McpWikiDraftError(
                400, "LEXICAL_OVERLAP_REJECTED", "Statement does not meet the configured lexical overlap policy."
            )


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"[^\W_]+", value, flags=re.UNICODE)}


def _bounded_id(value: str, field: str) -> str:
    if value != value.strip() or not value or len(value) > 255 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
        raise McpWikiDraftError(400, "INVALID_INPUT", f"{field} has an invalid format.")
    return value


def _plain_text(value: str, field: str, limit: int, policy: SubmissionPolicy) -> str:
    if value != value.strip() or not value or len(value) > limit:
        raise McpWikiDraftError(400, "INVALID_INPUT", f"{field} is blank, padded, or too long.")
    if re.search(r"(?i)\b(?:https?|ftp)://", value) or external_urls(value) or sanitize_ai_markdown(value) != value:
        raise McpWikiDraftError(400, "UNSAFE_CONTENT", f"{field} contains URL, image, or unsafe markup.")
    if any(pattern.search(value) for pattern in policy.instruction_patterns):
        raise McpWikiDraftError(400, "INSTRUCTION_PATTERN_REJECTED", f"{field} matches the configured instruction policy.")
    return value
