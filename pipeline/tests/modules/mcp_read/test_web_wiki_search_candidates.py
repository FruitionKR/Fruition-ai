"""MCP 검색이 웹 근거 선택과 같은 저장소·후보 순위를 쓰는지 확인한다."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.modules.mcp_read.application.wiki_search import SearchWikiUseCase
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

    def list_workspace_embedding_units(self, workspace_id: str):
        return [(item, f"Title {item.page_id}") for item in self.units]


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


def test_ranking_matches_web_evidence_selector_for_the_same_candidates() -> None:
    query = "refund window"
    web = JevEvidenceSelector(None, None, Units(UNITS), Dense(), Bm25Searcher())._candidates(
        query, "workspace-1"
    )
    expected: list[tuple[str, str]] = []
    for item in web:
        for page_id in item.page_ids:
            if page_id not in {page for page, _ in expected}:
                expected.append((page_id, item.unit.text))

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


def test_workspace_over_the_old_byte_cap_still_returns_results() -> None:
    # 예전 MCP 전용 상한(MCP_WIKI_SEARCH_MAX_CANDIDATE_BYTES=262144)을 넘는 unit 본문이다.
    large = [unit(f"u{index:04}", f"page-{index:04}", f"refund {index} " + "x" * 2000, f"B{index:04}") for index in range(300)]
    repository = WebWikiSearchCandidates(Dense(), Bm25Searcher(), wiki_repository=Units(large))
    assert sum(len(item.text.encode()) for item in large) > 262144

    with patch.object(database, "connect", return_value=PageConnection()):
        response = service(repository).execute(request(query="refund"))

    assert len(response["results"]) == 1
    assert response["next_cursor"] is not None


class CountingJev:
    def __init__(self) -> None:
        self.judged = 0

    def choose(self, state: str, questions: dict) -> dict:
        self.judged += len(questions)
        return {key: {"choice": "exclude", "probabilities": {"exclude": 1.0}} for key in questions}


def test_jev_judges_the_same_number_of_candidates_as_the_web_selector() -> None:
    units = [unit(f"u{index:04}", f"page-{index:04}", f"refund case {index}", f"B{index:04}") for index in range(250)]
    web_jev, mcp_jev = CountingJev(), CountingJev()
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

    assert web_jev.judged == mcp_jev.judged == CANDIDATE_LIMIT == 200


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
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
