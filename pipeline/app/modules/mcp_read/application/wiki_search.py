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

from app.core.pipeline_control import ensure_task_active, request_deadline
from app.modules.query.application.source_references import remove_block_refs, source_references_from_ids
from app.modules.query.domain.entities import SemanticQueryEmbedding
from app.modules.query.infrastructure.jev_evidence_selector import EvidenceCandidate


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
    # document가 사용자 크레딧으로 Jev 비용을 낼 수 있다고 판정했을 때만 True다. 없으면 Jev 없이 하이브리드 순서만 쓴다.
    jev_allowed: bool = False


@dataclass(frozen=True)
class WikiSearchPage:
    page_id: str
    page_type: str
    title: str
    version: int
    content_hash: str


@dataclass(frozen=True)
class WikiSearchCandidates:
    # 웹 근거 선택과 같은 unit 후보를 같은 순서로 담는다. 한 후보가 여러 page를 가리킬 수 있다.
    ranked: list[EvidenceCandidate]
    # 후보가 가리키는 active page의 결과 metadata. 없는 page는 결과에서 뺀다.
    pages: dict[str, WikiSearchPage]


class WikiSearchRepositoryPort(Protocol):
    def list_candidates(
        self,
        *,
        workspace_id: str,
        query: str,
        candidate_limit: int,
        materialized_bytes_limit: int,
        scan_page_limit: int,
        scan_unit_limit: int,
    ) -> WikiSearchCandidates: ...


class WikiSearchEmbeddingPort(Protocol):
    def embed_query(self, query: str) -> SemanticQueryEmbedding: ...


class WikiSearchEvidencePort(Protocol):
    def select(self, actor: WikiSearchActor, query: str, candidates: list[EvidenceCandidate]) -> list[str]:
        """근거로 고른 후보 unit ID를 고른 순서로 돌려준다. 쓸 수 없으면 빈 목록이다."""
        ...


class WikiSearchError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


class SearchWikiUseCase:
    _ORDER = "mcp-wiki-web-evidence-v2"
    _HASH = re.compile(r"^sha256:[0-9a-f]{64}$")

    def __init__(
        self,
        repository: WikiSearchRepositoryPort,
        embedding_search: WikiSearchEmbeddingPort,
        limits: WikiSearchLimits,
        cursor_key: bytes,
        query_embedding_model: str,
        track_query_embedding: Callable[[WikiSearchActor, str, str], AbstractContextManager] | None = None,
        evidence_selector: WikiSearchEvidencePort | None = None,
    ) -> None:
        self._repository = repository
        self._embedding_search = embedding_search
        self.limits = limits
        self._cursor_key = cursor_key
        self._query_embedding_model = query_embedding_model
        self._track_query_embedding = track_query_embedding or (lambda _actor, _model, _query: nullcontext())
        self._evidence_selector = evidence_selector

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
                candidates = self._repository.list_candidates(
                    workspace_id=request.actor.workspace_id,
                    query=request.query,
                    candidate_limit=self.limits.max_candidates,
                    materialized_bytes_limit=self.limits.max_candidate_bytes,
                    scan_page_limit=self.limits.max_scan_pages,
                    scan_unit_limit=self.limits.max_scan_units,
                )
                self._validate_prefetch(candidates)
                hits = self._page_hits(candidates)
                if cursor is None:
                    ranked = self._selected_first(request, candidates.ranked, hits)
                else:
                    # 이어지는 요청은 Jev를 다시 부르지 않고 cursor에 고정한 순서를 현재 후보와 대조해 쓴다.
                    ranked = self._frozen_order(cursor, candidates.ranked, hits)
                ensure_task_active()
        except WikiSearchError:
            raise
        except Exception as exc:
            from app.core.pipeline_control import DeadlineExceededError

            if isinstance(exc, DeadlineExceededError):
                raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki search deadline exceeded.", retryable=True) from None
            raise

        snapshot = [self._cursor_item(hit) for hit in ranked]
        offset = int(cursor["offset"]) if cursor is not None else 0

        selected = ranked[offset:offset + request.page_size]
        next_offset = offset + len(selected)
        next_cursor = None
        if next_offset < len(ranked):
            next_cursor = self._encode_cursor(request, next_offset, snapshot)

        response: dict[str, object] = {
            "schema_version": "mcp.wiki-search.v1",
            "results": [self._result(hit) for hit in selected],
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

    def _validate_prefetch(self, candidates: WikiSearchCandidates) -> None:
        if len(candidates.ranked) > self.limits.max_candidates:
            raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki candidate repository exceeded its row bound.")
        for page in candidates.pages.values():
            if page.page_type not in {"source", "concept"}:
                raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki candidate has an invalid page type.")
            if not page.page_id or len(page.page_id) > 255 or page.version < 1:
                raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki candidate has an invalid identity.")
            if len(page.title) > self.limits.max_string_chars:
                raise WikiSearchError(413, "RESPONSE_LIMIT_EXCEEDED", "Wiki page title exceeds the configured limit.")
            if not self._HASH.fullmatch(page.content_hash):
                raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki candidate has an invalid content hash.")

    def _validate_query_embedding(self, semantic_query: SemanticQueryEmbedding) -> None:
        vector = semantic_query.vector
        if (
            semantic_query.model_name != self._query_embedding_model
            or not vector
            or len(vector) > self.limits.max_vector_dimensions
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
        ):
            raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Embedding provider returned an invalid result.")

    def _page_hits(self, candidates: WikiSearchCandidates) -> list[tuple[WikiSearchPage, EvidenceCandidate]]:
        """unit 후보 순서대로 처음 나온 page를 결과로 둔다. snippet·근거는 그 page를 처음 가리킨 unit이다."""
        hits: dict[str, tuple[WikiSearchPage, EvidenceCandidate]] = {}
        for candidate in candidates.ranked:
            for page_id in candidate.page_ids:
                page = candidates.pages.get(page_id)
                if page is not None and page_id not in hits:
                    hits[page_id] = (page, candidate)
        return list(hits.values())[: self.limits.max_candidates]

    def _selected_first(
        self,
        request: WikiSearchRequest,
        ranked: list[EvidenceCandidate],
        hits: list[tuple[WikiSearchPage, EvidenceCandidate]],
    ) -> list[tuple[WikiSearchPage, EvidenceCandidate]]:
        if not request.jev_allowed or self._evidence_selector is None or not hits:
            return hits
        ensure_task_active()
        selected_ids = self._evidence_selector.select(request.actor, request.query, ranked)
        units = {candidate.unit.id: candidate for candidate in ranked}
        pages = {page.page_id: page for page, _ in hits}
        # 후보에 없는 ID는 버려, 판정 결과가 새 page를 만들어내지 못하게 한다. 고른 unit이 그 page의 snippet이 된다.
        front: dict[str, tuple[WikiSearchPage, EvidenceCandidate]] = {}
        for unit_id in selected_ids:
            candidate = units.get(unit_id)
            for page_id in candidate.page_ids if candidate is not None else ():
                if page_id in pages and page_id not in front:
                    front[page_id] = (pages[page_id], candidate)
        return list(front.values()) + [hit for hit in hits if hit[0].page_id not in front]

    def _frozen_order(
        self,
        cursor: dict[str, object],
        ranked: list[EvidenceCandidate],
        hits: list[tuple[WikiSearchPage, EvidenceCandidate]],
    ) -> list[tuple[WikiSearchPage, EvidenceCandidate]]:
        items = cursor["items"]
        pages = {page.page_id: page for page, _ in hits}
        units = {candidate.unit.id: candidate for candidate in ranked}
        ordered: list[tuple[WikiSearchPage, EvidenceCandidate]] = []
        for item in items:
            page = pages.get(item.get("page_id")) if isinstance(item, dict) else None
            candidate = units.get(item.get("unit_id")) if isinstance(item, dict) else None
            if page is None or candidate is None or page.page_id not in candidate.page_ids:
                break
            ordered.append((page, candidate))
        if len(hits) != len(items) or len(ordered) != len(items) or any(
            self._cursor_item(hit) != item for hit, item in zip(ordered, items, strict=True)
        ):
            raise WikiSearchError(409, "STALE_CURSOR", "Wiki search results changed; start a new search.")
        return ordered

    def _evidence(self, candidate: EvidenceCandidate) -> list[dict[str, str]]:
        refs = source_references_from_ids(list(candidate.unit.source_block_ids), candidate.unit.source_document_id)
        if len(refs) > self.limits.max_candidates:
            raise WikiSearchError(413, "RESPONSE_LIMIT_EXCEEDED", "Wiki evidence exceeds the configured limit.")
        evidence = []
        for ref in refs:
            if not ref.source_document_id or len(ref.source_document_id) > 255:
                raise WikiSearchError(503, "DEPENDENCY_UNAVAILABLE", "Wiki evidence has an invalid document identity.")
            evidence.append({"document_id": ref.source_document_id, "block_id": ref.source_block_id})
        return evidence

    def _result(self, hit: tuple[WikiSearchPage, EvidenceCandidate]) -> dict[str, object]:
        page, candidate = hit
        snippet = " ".join(remove_block_refs(candidate.unit.text).split())
        return {
            "page_id": page.page_id,
            "page_type": page.page_type.upper(),
            "title": page.title,
            "version": page.version,
            "content_hash": page.content_hash,
            "snippet": snippet[: self.limits.max_string_chars],
            "evidence": self._evidence(candidate),
        }

    def _cursor_item(self, hit: tuple[WikiSearchPage, EvidenceCandidate]) -> dict[str, object]:
        page, candidate = hit
        return {
            "page_id": page.page_id,
            "version": page.version,
            "content_hash": page.content_hash,
            "unit_id": candidate.unit.id,
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
