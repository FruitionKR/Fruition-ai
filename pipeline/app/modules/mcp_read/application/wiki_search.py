from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

from app.core.pipeline_control import ensure_task_active, remaining_seconds, request_deadline
from app.modules.query.application.source_references import remove_block_refs, source_references_from_ids
from app.modules.query.domain.entities import SemanticQueryEmbedding
from app.modules.query.domain.scoring import evidence_embedding_weight, hybrid_score


@dataclass(frozen=True)
class WikiSearchLimits:
    max_string_chars: int
    max_page_size: int
    max_candidates: int
    max_candidate_bytes: int
    deadline_ms: int
    max_request_bytes: int
    max_response_bytes: int
    max_scan_pages: int
    max_scan_units: int
    max_vector_dimensions: int


@dataclass(frozen=True)
class WikiSearchActor:
    invocation_id: str
    grant_id: str
    user_id: str
    workspace_id: str
    client_id: str
    policy_generation: int
    origin: str
    spec_version: str
    tool_name: str


@dataclass(frozen=True)
class WikiSearchRequest:
    actor: WikiSearchActor
    query: str
    cursor: str | None
    page_size: int


@dataclass(frozen=True)
class WikiSearchCandidate:
    page_id: str
    page_type: str
    title: str
    summary: str
    version: int
    content_hash: str
    embedding_text: str
    source_document_id: str | None
    block_refs: tuple[str, ...]
    evidence_count: int
    materialized_bytes: int
    semantic_score: float = 0.0


class WikiSearchRepositoryPort(Protocol):
    def list_candidates(
        self,
        *,
        workspace_id: str,
        query: str,
        semantic_query: SemanticQueryEmbedding,
        candidate_limit: int,
        materialized_bytes_limit: int,
        evidence_limit: int,
        statement_timeout_ms: int,
        scan_page_limit: int,
        scan_unit_limit: int,
        vector_dimension_limit: int,
    ) -> list[WikiSearchCandidate]: ...


class WikiSearchEmbeddingPort(Protocol):
    def embed_query(self, query: str) -> SemanticQueryEmbedding: ...


class WikiSearchTextPort(Protocol):
    def score(self, query: str, documents: list[str]) -> list[float]: ...


class WikiSearchError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


class SearchWikiUseCase:
    _ORDER = "mcp-wiki-hybrid-v1"
    _HASH = re.compile(r"^sha256:[0-9a-f]{64}$")

    def __init__(
        self,
        repository: WikiSearchRepositoryPort,
        embedding_search: WikiSearchEmbeddingPort,
        text_search: WikiSearchTextPort,
        limits: WikiSearchLimits,
        cursor_key: bytes,
        query_embedding_model: str,
        track_query_embedding: Callable[[WikiSearchActor, str, str], AbstractContextManager] | None = None,
    ) -> None:
        self._repository = repository
        self._embedding_search = embedding_search
        self._text_search = text_search
        self.limits = limits
        self._cursor_key = cursor_key
        self._query_embedding_model = query_embedding_model
        self._track_query_embedding = track_query_embedding or (lambda _actor, _model, _query: nullcontext())

    def execute(self, request: WikiSearchRequest) -> dict[str, object]:
        self._validate_request(request)
        cursor = self._decode_cursor(request.cursor) if request.cursor is not None else None
        if cursor is not None:
            self._validate_cursor_binding(cursor, request)

        try:
            with request_deadline(self.limits.deadline_ms / 1000):
                ensure_task_active()
                with self._track_query_embedding(request.actor, self._query_embedding_model, request.query):
                    semantic_query = self._embedding_search.embed_query(request.query)
                self._validate_query_embedding(semantic_query)
                ensure_task_active()
                remaining = remaining_seconds()
                statement_timeout_ms = max(1, min(self.limits.deadline_ms, int((remaining or 0) * 1000)))
                candidates = self._repository.list_candidates(
                    workspace_id=request.actor.workspace_id,
                    query=request.query,
                    semantic_query=semantic_query,
                    candidate_limit=self.limits.max_candidates,
                    materialized_bytes_limit=self.limits.max_candidate_bytes,
                    evidence_limit=self.limits.max_candidates,
                    statement_timeout_ms=statement_timeout_ms,
                    scan_page_limit=self.limits.max_scan_pages,
                    scan_unit_limit=self.limits.max_scan_units,
                    vector_dimension_limit=self.limits.max_vector_dimensions,
                )
                self._validate_prefetch(candidates)
                ranked = self._rank(request.query, candidates)
                ensure_task_active()
        except WikiSearchError:
            raise
        except Exception as exc:
            from app.core.pipeline_control import DeadlineExceededError

            if isinstance(exc, DeadlineExceededError):
                raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki search deadline exceeded.", retryable=True) from None
            raise

        snapshot = [self._cursor_item(candidate) for candidate in ranked]
        offset = 0
        if cursor is not None:
            if cursor.get("items") != snapshot:
                raise WikiSearchError(409, "STALE_CURSOR", "Wiki search results changed; start a new search.")
            offset = int(cursor["offset"])

        selected = ranked[offset:offset + request.page_size]
        next_offset = offset + len(selected)
        next_cursor = None
        if next_offset < len(ranked):
            next_cursor = self._encode_cursor(request, next_offset, snapshot)

        response: dict[str, object] = {
            "schema_version": "mcp.wiki-search.v1",
            "results": [self._result(candidate) for candidate in selected],
            "next_cursor": next_cursor,
            "truncated": next_cursor is not None,
            "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT",
        }
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.limits.max_response_bytes:
            raise WikiSearchError(413, "RESPONSE_LIMIT_EXCEEDED", "Wiki search response exceeds the configured limit.")
        return response

    def _validate_request(self, request: WikiSearchRequest) -> None:
        strings = (
            request.actor.invocation_id,
            request.actor.grant_id,
            request.actor.user_id,
            request.actor.workspace_id,
            request.actor.client_id,
            request.actor.origin,
            request.actor.spec_version,
            request.actor.tool_name,
            request.query,
        )
        if any(not value or len(value) > self.limits.max_string_chars for value in strings):
            raise WikiSearchError(400, "INVALID_INPUT", "A required string is blank or exceeds the configured limit.")
        if request.cursor is not None and len(request.cursor) > self.limits.max_request_bytes:
            raise WikiSearchError(400, "INVALID_HANDLE_FORMAT", "Cursor exceeds the configured limit.")
        if request.page_size < 1 or request.page_size > self.limits.max_page_size:
            raise WikiSearchError(400, "INVALID_INPUT", "page_size is outside the configured range.")

    def _validate_prefetch(self, candidates: list[WikiSearchCandidate]) -> None:
        if len(candidates) > self.limits.max_candidates:
            raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki candidate repository exceeded its row bound.")
        if sum(candidate.materialized_bytes for candidate in candidates) > self.limits.max_candidate_bytes:
            raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki candidate repository exceeded its byte bound.")
        for candidate in candidates:
            if candidate.page_type not in {"source", "concept"}:
                raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki candidate has an invalid page type.")
            if not candidate.page_id or len(candidate.page_id) > 255 or candidate.version < 1:
                raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki candidate has an invalid identity.")
            if len(candidate.title) > self.limits.max_string_chars:
                raise WikiSearchError(413, "RESPONSE_LIMIT_EXCEEDED", "Wiki page title exceeds the configured limit.")
            if not self._HASH.fullmatch(candidate.content_hash):
                raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki candidate has an invalid content hash.")
            if candidate.evidence_count > self.limits.max_candidates:
                raise WikiSearchError(413, "RESPONSE_LIMIT_EXCEEDED", "Wiki evidence exceeds the configured limit.")

    def _validate_query_embedding(self, semantic_query: SemanticQueryEmbedding) -> None:
        vector = semantic_query.vector
        if (
            semantic_query.model_name != self._query_embedding_model
            or not vector
            or len(vector) > self.limits.max_vector_dimensions
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
        ):
            raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Embedding provider returned an invalid result.")

    def _rank(self, query: str, candidates: list[WikiSearchCandidate]) -> list[WikiSearchCandidate]:
        if not candidates:
            return []
        lexical_texts = ["\n".join((candidate.title, candidate.summary, candidate.embedding_text)) for candidate in candidates]
        ensure_task_active()
        text_scores = self._text_search.score(query, lexical_texts)
        ensure_task_active()
        if len(text_scores) != len(candidates):
            raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki scorer returned an invalid result count.")
        weight = evidence_embedding_weight(query)
        scores = {
            candidate.page_id: hybrid_score(candidate.semantic_score, lexical, embedding_weight=weight)
            for candidate, lexical in zip(candidates, text_scores, strict=True)
        }
        return sorted(candidates, key=lambda candidate: (-scores[candidate.page_id], -candidate.version, candidate.page_id))

    def _evidence(self, candidate: WikiSearchCandidate) -> list[dict[str, str]]:
        refs = source_references_from_ids(list(candidate.block_refs), candidate.source_document_id)
        evidence = []
        for ref in refs:
            if not ref.source_document_id or len(ref.source_document_id) > 255:
                raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki evidence has an invalid document identity.")
            evidence.append({"document_id": ref.source_document_id, "block_id": ref.source_block_id})
        return evidence

    def _result(self, candidate: WikiSearchCandidate) -> dict[str, object]:
        snippet = " ".join(remove_block_refs(candidate.embedding_text or candidate.summary or candidate.title).split())
        return {
            "page_id": candidate.page_id,
            "page_type": candidate.page_type.upper(),
            "title": candidate.title,
            "version": candidate.version,
            "content_hash": candidate.content_hash,
            "snippet": snippet[: self.limits.max_string_chars],
            "evidence": self._evidence(candidate),
        }

    def _cursor_item(self, candidate: WikiSearchCandidate) -> dict[str, object]:
        return {
            "page_id": candidate.page_id,
            "version": candidate.version,
            "content_hash": candidate.content_hash,
            "evidence": [[item["document_id"], item["block_id"]] for item in self._evidence(candidate)],
        }

    def _encode_cursor(self, request: WikiSearchRequest, offset: int, items: list[dict[str, object]]) -> str:
        actor = request.actor
        payload = {
            "v": 1,
            "user_id": actor.user_id,
            "workspace_id": actor.workspace_id,
            "grant_id": actor.grant_id,
            "client_id": actor.client_id,
            "policy_generation": actor.policy_generation,
            "origin": actor.origin,
            "spec_version": actor.spec_version,
            "tool_name": actor.tool_name,
            "query_hash": hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
            "page_size": request.page_size,
            "order": self._ORDER,
            "offset": offset,
            "items": items,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self._cursor_key, raw, hashlib.sha256).digest()
        token = f"{self._b64(raw)}.{self._b64(signature)}"
        continuation = {
            "schema_version": "mcp.wiki-search.v1",
            "actor": asdict(request.actor),
            "query": request.query,
            "cursor": token,
            "page_size": request.page_size,
        }
        continuation_bytes = json.dumps(
            continuation,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(continuation_bytes) > self.limits.max_request_bytes:
            raise WikiSearchError(413, "RESPONSE_LIMIT_EXCEEDED", "Wiki search cursor exceeds the configured limit.")
        return token

    def _decode_cursor(self, token: str) -> dict[str, object]:
        try:
            encoded_payload, encoded_signature = token.split(".")
            raw = self._unb64(encoded_payload)
            signature = self._unb64(encoded_signature)
            expected = hmac.new(self._cursor_key, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("v") != 1:
                raise ValueError("payload")
            if type(payload.get("offset")) is not int or payload["offset"] < 1:
                raise ValueError("offset")
            if not isinstance(payload.get("items"), list):
                raise ValueError("items")
            return payload
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise WikiSearchError(404, "HANDLE_NOT_FOUND", "Cursor was not found.") from None

    def _validate_cursor_binding(self, cursor: dict[str, object], request: WikiSearchRequest) -> None:
        actor = request.actor
        binding = {
            "user_id": actor.user_id,
            "workspace_id": actor.workspace_id,
            "grant_id": actor.grant_id,
            "client_id": actor.client_id,
            "policy_generation": actor.policy_generation,
            "origin": actor.origin,
            "spec_version": actor.spec_version,
            "tool_name": actor.tool_name,
            "query_hash": hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
            "page_size": request.page_size,
            "order": self._ORDER,
        }
        if any(cursor.get(key) != value for key, value in binding.items()):
            raise WikiSearchError(404, "HANDLE_NOT_FOUND", "Cursor was not found.")

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _unb64(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
