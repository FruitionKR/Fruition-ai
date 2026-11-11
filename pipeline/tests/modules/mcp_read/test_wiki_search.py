from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import json
from unittest.mock import patch

import pytest

from app.modules.mcp_read.application.wiki_search import (
    SearchWikiUseCase,
    WikiSearchActor,
    WikiSearchCandidates,
    WikiSearchError,
    WikiSearchLimits,
    WikiSearchPage,
    WikiSearchRequest,
)
from app.modules.mcp_read.infrastructure.web_wiki_search_candidates import WebWikiSearchCandidates
from app.modules.query.domain.entities import SemanticQueryEmbedding, WikiEmbeddingUnit
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.query.infrastructure.jev_evidence_selector import EvidenceCandidate
from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch


HASH = "sha256:" + "a" * 64
ACTOR = WikiSearchActor(
    invocation_id="123e4567-e89b-42d3-a456-426614174000",
    grant_id="123e4567-e89b-42d3-b456-426614174001",
    user_id="user-1",
    workspace_id="workspace-1",
    client_id="https://client.example/cimd.json",
    policy_generation=1,
    origin="MCP_CLIENT",
    spec_version="2025-11-25",
    tool_name="search_wiki",
)


def limits(**values: int) -> WikiSearchLimits:
    defaults = {
        "max_string_chars": 512,
        "max_page_size": 2,
        "deadline_ms": 1000,
        "max_request_bytes": 16384,
        "max_response_bytes": 16384,
        "max_vector_dimensions": 16,
    }
    defaults.update(values)
    return WikiSearchLimits(**defaults)


def candidate(
    page_id: str,
    *,
    document_id: str = "doc-a",
    block_id: str = "B0001",
    version: int = 1,
    text: str = "query evidence",
    page_type: str = "concept",
    title: str | None = None,
    content_hash: str = HASH,
) -> tuple[EvidenceCandidate, WikiSearchPage]:
    """웹 근거 후보 하나와 그 page metadata."""
    unit = WikiEmbeddingUnit(
        id=f"unit-{page_id}",
        page_id=page_id,
        source_document_id=document_id,
        unit_type="evidence",
        source_block_ids=[block_id],
        text=text,
    )
    page = WikiSearchPage(
        page_id=page_id,
        page_type=page_type,
        title=title if title is not None else f"Title {page_id}",
        version=version,
        content_hash=content_hash,
    )
    return EvidenceCandidate(unit=unit, source_titles=(page.title,), page_ids=(page_id,)), page


class Repository:
    """후보를 받은 순서(웹 후보 순위) 그대로 돌려준다."""

    def __init__(self, candidates: list[tuple[EvidenceCandidate, WikiSearchPage]]) -> None:
        self.candidates = candidates
        self.calls: list[dict] = []

    def list_candidates(self, **kwargs) -> WikiSearchCandidates:
        self.calls.append(kwargs)
        return WikiSearchCandidates(
            # 같은 묶음 후보를 여러 page가 공유하면 한 번만 둔다.
            ranked=list({id(item): item for item, _ in self.candidates}.values()),
            pages={page.page_id: page for _, page in self.candidates},
        )


class Search:
    def embed_query(self, query: str) -> SemanticQueryEmbedding:
        return SemanticQueryEmbedding(model_name="test-embedding", vector=[1.0, 0.0])


def service(repository, embedding=None, *, search_limits=None, tracker=None) -> SearchWikiUseCase:
    return SearchWikiUseCase(
        repository=repository,
        embedding_search=embedding or Search(),
        limits=search_limits or limits(),
        cursor_key=b"cursor-test-key-that-is-at-least-32-bytes",
        query_embedding_model="test-embedding",
        track_query_embedding=tracker,
    )


def request(
    *, actor: WikiSearchActor = ACTOR, cursor: str | None = None, page_size: int = 1, query: str = "query",
    jev_allowed: bool = False,
):
    return WikiSearchRequest(actor=actor, query=query, cursor=cursor, page_size=page_size, jev_allowed=jev_allowed)


def test_paginates_with_document_qualified_collision_safe_evidence() -> None:
    repository = Repository([
        candidate("page-a", document_id="doc-a"),
        candidate("page-b", document_id="doc-b"),
    ])
    use_case = service(repository)

    first = use_case.execute(request())
    second = use_case.execute(request(cursor=first["next_cursor"]))

    assert first["truncated"] is True
    assert second["truncated"] is False
    assert first["results"][0]["evidence"] == [{"document_id": "doc-a", "block_id": "B0001"}]
    assert second["results"][0]["evidence"] == [{"document_id": "doc-b", "block_id": "B0001"}]
    assert repository.calls[0]["workspace_id"] == "workspace-1"
    # 후보 조회에는 MCP 전용 상한을 넘기지 않는다.
    assert all(call == {"workspace_id": "workspace-1", "query": "query"} for call in repository.calls)


@pytest.mark.parametrize(
    "changed_actor",
    [replace(ACTOR, user_id="user-2"), replace(ACTOR, workspace_id="workspace-2"), replace(ACTOR, grant_id="123e4567-e89b-42d3-8456-426614174002")],
)
def test_cursor_cannot_be_replayed_across_actor_boundaries(changed_actor: WikiSearchActor) -> None:
    use_case = service(Repository([candidate("page-a"), candidate("page-b")]))
    cursor = use_case.execute(request())["next_cursor"]

    with pytest.raises(WikiSearchError) as exc:
        use_case.execute(request(actor=changed_actor, cursor=cursor))

    assert (exc.value.status_code, exc.value.code) == (404, "HANDLE_NOT_FOUND")


def test_cursor_rejects_tampering_and_argument_changes() -> None:
    use_case = service(Repository([candidate("page-a"), candidate("page-b")]))
    cursor = use_case.execute(request())["next_cursor"]

    with pytest.raises(WikiSearchError, match="not found") as tampered:
        use_case.execute(request(cursor=cursor[:-1] + ("A" if cursor[-1] != "A" else "B")))
    assert tampered.value.code == "HANDLE_NOT_FOUND"

    with pytest.raises(WikiSearchError) as changed:
        use_case.execute(request(cursor=cursor, query="query "))
    assert changed.value.code == "HANDLE_NOT_FOUND"


def test_cursor_keeps_every_candidate_page_without_a_candidate_cap() -> None:
    use_case = service(Repository([candidate(f"page-{index:03}", block_id=f"B{index:04}") for index in range(250)]))

    ids: list[str] = []
    cursor = None
    while True:
        response = use_case.execute(request(cursor=cursor, page_size=2))
        ids.extend(item["page_id"] for item in response["results"])
        cursor = response["next_cursor"]
        if cursor is None:
            break

    assert ids == [f"page-{index:03}" for index in range(250)]


def test_cursor_revalidates_page_versions_hashes_and_order() -> None:
    repository = Repository([candidate("page-a"), candidate("page-b")])
    use_case = service(repository)
    cursor = use_case.execute(request())["next_cursor"]
    repository.candidates[1] = candidate("page-b", version=2)

    with pytest.raises(WikiSearchError) as exc:
        use_case.execute(request(cursor=cursor))

    assert (exc.value.status_code, exc.value.code) == (409, "STALE_CURSOR")


def test_rejects_invalid_sizes_and_invalid_page_metadata() -> None:
    use_case = service(Repository([candidate(f"page-{index}") for index in range(5)]))
    with pytest.raises(WikiSearchError) as page_size:
        use_case.execute(request(page_size=3))
    assert page_size.value.code == "INVALID_INPUT"

    with pytest.raises(WikiSearchError) as page_type:
        service(Repository([candidate("page-a", page_type="web")])).execute(request())
    assert page_type.value.code == "DEPENDENCY_UNAVAILABLE"

    with pytest.raises(WikiSearchError) as content_hash:
        service(Repository([candidate("page-a", content_hash="sha256:bad")])).execute(request())
    assert content_hash.value.code == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.parametrize("vector", [[], [1.0, 2.0, 3.0], [1.0, float("nan")]])
def test_rejects_invalid_remote_query_vectors_before_database(vector: list[float]) -> None:
    class InvalidEmbedding:
        def embed_query(self, _query: str) -> SemanticQueryEmbedding:
            return SemanticQueryEmbedding("test-embedding", vector)

    repository = Repository([])
    with pytest.raises(WikiSearchError) as exc:
        service(
            repository,
            embedding=InvalidEmbedding(),
            search_limits=limits(max_vector_dimensions=2),
        ).execute(request())

    assert (exc.value.code, exc.value.retryable) == ("DEPENDENCY_UNAVAILABLE", False)
    assert repository.calls == []


def test_response_bytes_and_strings_are_bounded() -> None:
    long_title = candidate("page-a", title="x" * 513)
    with pytest.raises(WikiSearchError) as title:
        service(Repository([long_title])).execute(request())
    assert title.value.code == "RESPONSE_LIMIT_EXCEEDED"

    snippet = service(Repository([candidate("page-a", text="x" * 513)])).execute(request())
    assert snippet["results"][0]["snippet"] == "x" * 512

    with pytest.raises(WikiSearchError) as response:
        service(Repository([candidate("page-a")]), search_limits=limits(max_response_bytes=20)).execute(request())
    assert response.value.code == "RESPONSE_LIMIT_EXCEEDED"


def test_every_emitted_cursor_fits_the_complete_continuation_request() -> None:
    candidates = [candidate("page-a"), candidate("page-b")]
    first = service(Repository(candidates)).execute(request())
    cursor = first["next_cursor"]
    continuation = {
        "schema_version": "mcp.wiki-search.v1",
        "actor": asdict(ACTOR),
        "query": "query",
        "cursor": cursor,
        "page_size": 1,
    }
    compact = json.dumps(continuation, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(compact) <= limits().max_request_bytes

    cursor_only_limit = len(cursor.encode()) + 1
    with pytest.raises(WikiSearchError) as exc:
        service(
            Repository(candidates),
            search_limits=limits(max_request_bytes=cursor_only_limit),
        ).execute(request())
    assert exc.value.code == "RESPONSE_LIMIT_EXCEEDED"


def test_only_query_is_embedded_and_usage_scope_is_separate() -> None:
    class Model:
        model_name = "test-embedding"

        def __init__(self) -> None:
            self.texts: list[str] = []

        def embed(self, texts: list[str]) -> list[list[float]]:
            self.texts.extend(texts)
            return [[1.0, 0.0] for _ in texts]

    class Rows:
        def __init__(self, rows) -> None:
            self._rows = rows

        def fetchall(self):
            return self._rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, _params):
            # 저장 벡터는 없고 page metadata만 있다.
            if "FROM wiki_pages" in sql:
                return Rows([{"id": "page-a", "page_type": "concept", "title": "Title page-a", "version": 1}])
            return Rows([])

    class Units:
        def list_workspace_embedding_units(self, workspace_id, limit=None):
            item, _page = candidate("page-a", text="body without stored vector")
            return [(item.unit, "Title page-a")]

    tracked: list[tuple[str, str, str]] = []

    @contextmanager
    def tracker(actor, model_name, query):
        tracked.append((actor.invocation_id, model_name, query))
        yield

    model = Model()
    embedding = StoredWikiPageEmbeddingSearch(model).without_live_embedding()
    repository = WebWikiSearchCandidates(embedding, Bm25Searcher(), wiki_repository=Units())
    with patch(
        "app.modules.wiki_ingestion.infrastructure.postgres_wiki_ingestion_repository.connect",
        return_value=Connection(),
    ):
        response = service(repository, embedding, tracker=tracker).execute(request())

    assert [item["page_id"] for item in response["results"]] == ["page-a"]
    assert model.texts == ["query"]
    assert tracked == [(ACTOR.invocation_id, "test-embedding", "query")]


def test_no_generative_answer_or_evaluator_path_is_constructed() -> None:
    repository = Repository([candidate("page-a")])
    with (
        patch("app.modules.query.interfaces.http.dependencies.build_answer_query_use_case", side_effect=AssertionError("generative path")),
        patch("app.modules.query.infrastructure.query_chat_answer_generator.build_query_chat_answer_generator", side_effect=AssertionError("answer generator")),
        patch("app.modules.query.infrastructure.query_answer_evaluator.build_query_answer_evaluator", side_effect=AssertionError("evaluator")),
    ):
        response = service(repository).execute(request())

    assert response["results"][0]["page_id"] == "page-a"
