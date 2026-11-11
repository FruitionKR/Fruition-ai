"""MCP 검색이 웹 근거 선택과 같은 저장소·후보 순위를 쓰는지 확인한다."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import QueryCanceled
from psycopg.rows import dict_row

from app.modules.mcp_read.application.wiki_search import SearchWikiUseCase, WikiSearchError
from app.modules.mcp_read.infrastructure.jev_wiki_search_evidence import JevWikiSearchEvidence
from app.modules.mcp_read.infrastructure.web_wiki_search_candidates import WebWikiSearchCandidates
from app.modules.query.domain.entities import WikiEmbeddingUnit
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.query.infrastructure.jev_evidence_selector import CANDIDATE_LIMIT, JevEvidenceSelector
from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database
from tests.modules.mcp_read.test_wiki_search import Search, limits, request, service


def unit(unit_id: str, page_id: str, text: str, block_id: str, document_id: str = "doc-a") -> WikiEmbeddingUnit:
    return WikiEmbeddingUnit(
        id=unit_id,
        page_id=page_id,
        source_document_id=document_id,
        unit_type="evidence",
        source_block_ids=[block_id],
        text=text,
    )


UNITS = [
    unit("u1", "page-a", "refund window is fourteen days", "B0001"),
    unit("u2", "page-b", "shipping takes three days", "B0002"),
    unit("u3", "page-b", "refund requires a receipt", "B0003"),
    unit("u4", "page-c", "refund window is fourteen days", "B0001"),
    unit("u5", "page-d", "office hours", "B0004"),
    unit("u6", "page-c", "partial refund for opened items", "B0005"),
]


class Units:
    def __init__(self, units: list[WikiEmbeddingUnit]) -> None:
        self.units = units
        self.calls: list[dict] = []

    def list_workspace_embedding_units(self, workspace_id: str, *, limit=None, statement_timeout_ms=None):
        self.calls.append({"limit": limit, "statement_timeout_ms": statement_timeout_ms})
        return [(item, f"Title {item.page_id}") for item in self.units][:limit]


class Dense:
    """본문별 고정 dense 점수. 웹 선택기와 MCP가 같은 값을 받는다."""

    SCORES = {"shipping takes three days": 0.9, "office hours": 0.8, "refund requires a receipt": 0.2}

    def embed_query(self, query: str):
        raise AssertionError("ranking must not re-embed through this fake")

    def score(self, query: str, documents: list[str]) -> list[float]:
        return [self.SCORES.get(document, 0.1) for document in documents]


class Rows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self):
        return self._rows


class PageConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query: str, params: tuple):
        if "set_config('statement_timeout'" in query:
            return Rows([])
        assert "FROM wiki_pages" in query
        workspace_id, page_ids = params
        return Rows([
            {"id": page_id, "page_type": "concept", "title": f"Title {page_id}", "version": 7}
            for page_id in page_ids
        ])


def all_results(use_case, query: str) -> list[dict]:
    results: list[dict] = []
    cursor = None
    while True:
        response = use_case.execute(request(query=query, cursor=cursor, page_size=2))
        results.extend(response["results"])
        cursor = response["next_cursor"]
        if cursor is None:
            return results


def first_pages(candidates) -> list[tuple[str, str]]:
    pages: list[tuple[str, str]] = []
    for item in candidates:
        for page_id in item.page_ids:
            if page_id not in {page for page, _ in pages}:
                pages.append((page_id, item.unit.text))
    return pages


@pytest.mark.parametrize("query", ["refund window", "환불 refund window"])
def test_without_jev_order_follows_the_web_fallback_language_weight(query: str) -> None:
    # 웹은 Jev 없이 근거를 고를 때 질문 언어별 키워드 비중(ADR 0009: 한국어 0.10, 그 밖 0.40)을 쓴다.
    keyword = 0.10 if "환불" in query else 0.40
    groups = JevEvidenceSelector(None, None, Units(UNITS), Dense(), Bm25Searcher(), candidate_limit=99)._candidates(
        query, "workspace-1"
    )
    texts = [item.unit.text for item in groups]
    scores = {
        item.unit.id: (1 - keyword) * dense + keyword * lexical
        for item, dense, lexical in zip(
            groups, Dense().score(query, texts), Bm25Searcher().score(query, texts), strict=True
        )
    }
    expected = first_pages(sorted(groups, key=lambda item: (-scores[item.unit.id], item.unit.id)))

    repository = WebWikiSearchCandidates(Dense(), Bm25Searcher(), wiki_repository=Units(UNITS))
    with patch.object(database, "connect", return_value=PageConnection()):
        results = all_results(service(repository, search_limits=limits(max_page_size=2)), query)

    assert [(item["page_id"], item["snippet"]) for item in results] == expected
    # 같은 본문·블록·문서(u1·u4)는 웹처럼 한 후보로 묶여 두 page를 함께 가리킨다.
    assert ("page-a", "refund window is fourteen days") in expected
    assert ("page-c", "refund window is fourteen days") in expected


def test_content_hash_follows_page_units_and_version_comes_from_the_page() -> None:
    def search(units):
        repository = WebWikiSearchCandidates(Dense(), Bm25Searcher(), wiki_repository=Units(units))
        with patch.object(database, "connect", return_value=PageConnection()):
            return {item["page_id"]: item for item in all_results(service(repository), "refund")}

    before = search(UNITS)
    after = search([*UNITS, unit("u7", "page-b", "new statement", "B0007")])

    assert before["page-b"]["version"] == 7
    assert before["page-b"]["content_hash"] != after["page-b"]["content_hash"]
    assert before["page-a"]["content_hash"] == after["page-a"]["content_hash"]


@pytest.mark.parametrize(
    ("bound", "value"),
    # UNITS는 6행, 본문 합 160 bytes 남짓이다.
    [("max_scan_units", 5), ("max_candidate_bytes", 100)],
)
def test_workspace_over_candidate_bounds_is_rejected_before_ranking(bound: str, value: int) -> None:
    class Unranked(Dense):
        def score(self, query, documents):
            raise AssertionError("ranking must not run over capacity")

    units = Units(UNITS)
    repository = WebWikiSearchCandidates(Unranked(), Bm25Searcher(), wiki_repository=units)

    with pytest.raises(WikiSearchError) as exc:
        service(repository, search_limits=limits(**{bound: value})).execute(request())

    assert (exc.value.status_code, exc.value.code, exc.value.retryable) == (503, "DEPENDENCY_UNAVAILABLE", False)
    # 행 상한+1개까지만 읽어 초과를 판정한다. 일부만 검색하지 않는다.
    assert units.calls[0]["limit"] == limits(**{bound: value}).max_scan_units + 1


def test_workspace_exactly_at_candidate_bounds_is_searched() -> None:
    total = sum(len(item.text.encode()) for item in UNITS)
    repository = WebWikiSearchCandidates(Dense(), Bm25Searcher(), wiki_repository=Units(UNITS))

    with patch.object(database, "connect", return_value=PageConnection()):
        response = service(repository, search_limits=limits(max_scan_units=6, max_candidate_bytes=total)).execute(
            request(query="refund")
        )

    assert response["results"]


def test_candidate_query_timeout_is_the_remaining_deadline_and_cancellation_fails_retryably() -> None:
    class Cancelled(Units):
        def list_workspace_embedding_units(self, workspace_id, *, limit=None, statement_timeout_ms=None):
            super().list_workspace_embedding_units(workspace_id, limit=limit, statement_timeout_ms=statement_timeout_ms)
            raise QueryCanceled("canceling statement due to statement timeout")

    units = Cancelled(UNITS)
    repository = WebWikiSearchCandidates(Dense(), Bm25Searcher(), wiki_repository=units)

    with pytest.raises(WikiSearchError) as exc:
        service(repository, search_limits=limits(deadline_ms=1000)).execute(request())

    assert (exc.value.status_code, exc.value.code, exc.value.retryable) == (503, "DEPENDENCY_UNAVAILABLE", True)
    assert 0 < units.calls[0]["statement_timeout_ms"] <= 1000


class CountingJev:
    def __init__(self) -> None:
        self.judged = 0

    def choose(self, state: str, questions: dict) -> dict:
        self.judged += len(questions)
        return {key: {"choice": "exclude", "probabilities": {"exclude": 1.0}} for key in questions}


class RecordingJev(CountingJev):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def choose(self, state: str, questions: dict) -> dict:
        self.ids.extend(questions)
        return super().choose(state, questions)


def test_jev_judges_the_same_candidates_as_the_web_selector() -> None:
    units = [unit(f"u{index:04}", f"page-{index:04}", f"refund case {index}", f"B{index:04}") for index in range(250)]
    web_jev, mcp_jev = RecordingJev(), RecordingJev()
    JevEvidenceSelector(web_jev, None, Units(units), Dense(), Bm25Searcher()).select("refund", [], {}, "workspace-1")

    repository = WebWikiSearchCandidates(Dense(), Bm25Searcher(), wiki_repository=Units(units))
    use_case = SearchWikiUseCase(
        repository=repository,
        embedding_search=Search(),
        limits=limits(),
        cursor_key=b"cursor-test-key-that-is-at-least-32-bytes",
        query_embedding_model="test-embedding",
        evidence_selector=JevWikiSearchEvidence(mcp_jev, max_selected=8),
    )
    with patch.object(database, "connect", return_value=PageConnection()):
        use_case.execute(request(query="refund", jev_allowed=True))

    # Jev 비용은 웹과 같이 사용자 크레딧으로 내므로 판정 후보도 웹과 같은 200개다.
    assert web_jev.judged == mcp_jev.judged == CANDIDATE_LIMIT == 200
    assert sorted(mcp_jev.ids) == sorted(web_jev.ids)


DSN = os.environ.get("TEST_AGENT_DATABASE_URL") or os.environ.get("CONCEPT_LOCK_TEST_DSN")
AI_SCHEMA_SQL = (Path(__file__).resolve().parents[3] / "db" / "ai_schema.sql").read_text(encoding="utf-8")


class QueryModel:
    model_name = "test-embedding"

    def embed(self, texts: list[str]) -> list[list[float]]:
        assert texts == ["what is the refund window"], "only the query may be embedded"
        return [[1.0, 0.0]]


@pytest.mark.skipif(not DSN, reason="isolated PostgreSQL test URL is not configured")
def test_postgres_finds_pages_by_unit_embeddings_without_a_page_embedding(monkeypatch) -> None:
    schema = "test_mcp_web_search_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in DSN else "?"
    scoped = f"{DSN}{separator}options={quote(f'-csearch_path={schema}')}"
    try:
        with psycopg.connect(scoped) as conn:
            conn.execute(AI_SCHEMA_SQL)
            for page_id, workspace_id, status, text, vector in (
                # page embedding이 없고 unit vector만 완료됐다. 키워드도 겹치지 않아 dense 점수로만 찾는다.
                ("page-dense", "workspace-1", "active", "returns accepted for two weeks", [1.0, 0.0]),
                ("page-other", "workspace-1", "active", "office hours", [0.0, 1.0]),
                ("page-deleted", "workspace-1", "deleted", "returns accepted for two weeks", [1.0, 0.0]),
                ("page-foreign", "workspace-other", "active", "returns accepted for two weeks", [1.0, 0.0]),
            ):
                conn.execute(
                    """
                    INSERT INTO wiki_pages
                        (id, page_type, title, slug, summary, user_id, workspace_id, status, created_at, updated_at)
                    VALUES (%s, 'concept', %s, %s, '', 'user', %s, %s, now(), now())
                    """,
                    (page_id, f"Title {page_id}", page_id, workspace_id, status),
                )
                vector_id = f"vector-{page_id}"
                conn.execute(
                    """
                    INSERT INTO wiki_embedding_vectors
                        (id, embedding_model, representation_hash, representation_text,
                         embedding_vector, embedding_dimension, status)
                    VALUES (%s, 'test-embedding', encode(sha256(convert_to(%s, 'UTF8')), 'hex'), %s, %s, 2, 'completed')
                    ON CONFLICT (embedding_model, representation_hash) DO NOTHING
                    """,
                    (vector_id, text, text, vector),
                )
                conn.execute(
                    """
                    INSERT INTO wiki_embedding_units
                        (id, embedding_vector_id, page_id, source_document_id, unit_type, block_refs, text)
                    SELECT %s, id, %s, %s, 'evidence', ARRAY['B0001'], %s
                    FROM wiki_embedding_vectors
                    WHERE embedding_model = 'test-embedding'
                      AND representation_hash = encode(sha256(convert_to(%s, 'UTF8')), 'hex')
                    """,
                    (f"unit-{page_id}", page_id, f"doc-{page_id}", text, text),
                )
            assert conn.execute("SELECT count(*) AS n FROM wiki_page_embeddings").fetchone()[0] == 0
        monkeypatch.setattr(database, "connect", lambda: psycopg.connect(scoped, row_factory=dict_row))

        embedding = StoredWikiPageEmbeddingSearch(QueryModel()).without_live_embedding()
        repository = WebWikiSearchCandidates(embedding, Bm25Searcher())
        use_case = service(repository, embedding, search_limits=limits(max_vector_dimensions=2))
        response = use_case.execute(request(query="what is the refund window", page_size=2))

        assert [item["page_id"] for item in response["results"]] == ["page-dense", "page-other"]
        assert response["results"][0]["evidence"] == [{"document_id": "doc-page-dense", "block_id": "B0001"}]
        assert response["results"][0]["snippet"] == "returns accepted for two weeks"
        assert response["results"][0]["version"] >= 1
        assert response["results"][0]["content_hash"].startswith("sha256:")

        # 후보 조회가 잠금에 막히면 남은 deadline의 statement timeout으로 끊고 재시도 가능한 오류로 끝낸다.
        with psycopg.connect(scoped) as locker:
            locker.execute("LOCK TABLE wiki_embedding_units IN ACCESS EXCLUSIVE MODE")
            started = time.monotonic()
            with pytest.raises(WikiSearchError) as exc:
                service(
                    repository, embedding, search_limits=limits(max_vector_dimensions=2, deadline_ms=300)
                ).execute(request(query="what is the refund window", page_size=2))
            elapsed = time.monotonic() - started
        assert (exc.value.code, exc.value.retryable) == ("DEPENDENCY_UNAVAILABLE", True)
        assert elapsed < 2
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
