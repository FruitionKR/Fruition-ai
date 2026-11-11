"""MCP 승인 적용은 웹 편입 반영 코드를 그대로 탄다: 임베딩·휴지통 해제·수동 본문 보존·품질 상태 초기화."""
import json
from uuid import uuid4

from psycopg.types.json import Jsonb

import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401
from app.modules.mcp_read.infrastructure.web_wiki_search_candidates import WebWikiSearchCandidates
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch
from app.modules.mcp_wiki_draft.interfaces.http import apply_routes
from app.modules.wiki_embedding.application.build_wiki_page_embeddings import BuildWikiPageEmbeddingsUseCase
from app.modules.wiki_embedding.infrastructure.minio_markdown_reader import MinioMarkdownReader
from app.modules.wiki_embedding.infrastructure.postgres_wiki_page_embedding_repository import (
    PostgresWikiPageEmbeddingRepository,
)
from app.modules.wiki_embedding.infrastructure.threaded_wiki_embedding_job import build_wiki_unit_embeddings


class _EmbeddingJob:
    def __init__(self):
        self.calls = []

    def start(self, run_id, page_ids):
        self.calls.append((run_id, list(page_ids)))


class _Model:
    model_name = "test-model"

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _apply_through_route(monkeypatch, grant, finalized):
    """운영 라우트의 조립(get_mcp_wiki_draft_apply)으로 적용한다. 승인 조회만 테스트 대역으로 바꾼다."""
    job = _EmbeddingJob()
    monkeypatch.setattr(apply_routes, "get_wiki_embedding_job", lambda: job, raising=False)
    monkeypatch.setattr(apply_routes, "ConfiguredWikiApprovalAuthority", h._ValidApprovalAuthority)
    apply_routes.get_mcp_wiki_draft_apply.cache_clear()
    try:
        payload = h._apply_payload(grant, finalized)
        result = apply_routes.get_mcp_wiki_draft_apply().apply(payload.draft_id, payload)
    finally:
        apply_routes.get_mcp_wiki_draft_apply.cache_clear()
    assert result["state"] == "PUBLISHED"
    return payload, job


def _ingest(monkeypatch, ledger=()):
    monkeypatch.setattr(h.calculation, "read_contributions", lambda *_args: list(ledger))
    grant = str(uuid4())
    intake = h._intake()
    started = h._complete_intake(intake, grant)
    finalized = intake.finalize(started["run_id"], h._finalize_payload(grant, started))
    return grant, started, finalized


def test_mcp_apply_starts_embedding_and_page_becomes_mcp_searchable(monkeypatch):
    grant, _started, finalized = _ingest(monkeypatch)
    payload, job = _apply_through_route(monkeypatch, grant, finalized)

    with h._connect() as conn:
        active = {
            str(row["id"]): row for row in conn.execute(
                "SELECT id,page_type,slug FROM wiki_pages WHERE status='active'"
            ).fetchall()
        }
    assert len(job.calls) == 1
    run_id, page_ids = job.calls[0]
    assert run_id == payload.operation_id
    assert set(page_ids) == set(active)

    # 임베딩 작업이 만든 벡터로 MCP search_wiki 후보 질의(웹 근거 검색 경로)가 발행한 개념을 찾는다.
    BuildWikiPageEmbeddingsUseCase(
        PostgresWikiPageEmbeddingRepository(), _Model(), MinioMarkdownReader(),
    ).execute(page_ids)
    build_wiki_unit_embeddings(page_ids, embedding_model=_Model())
    found = WebWikiSearchCandidates(
        StoredWikiPageEmbeddingSearch(_Model()).without_live_embedding(), Bm25Searcher(),
    ).list_candidates(
        workspace_id="workspace-1",
        query="Alpha concept",
        unit_limit=10_000,
        text_bytes_limit=10_000_000,
        statement_timeout_ms=5_000,
    )
    alpha = next(page_id for page_id, row in active.items() if row["slug"] == "alpha")
    assert alpha in found.pages


def test_mcp_reingest_clears_document_tombstone(monkeypatch):
    with h._connect() as conn:
        conn.execute("INSERT INTO wiki_source_tombstones(workspace_id,document_id) VALUES ('workspace-1','doc_1')")
    grant, _started, finalized = _ingest(monkeypatch)
    _apply_through_route(monkeypatch, grant, finalized)
    with h._connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM wiki_source_tombstones").fetchone()["n"] == 0
        source = conn.execute("SELECT status FROM wiki_pages WHERE page_type='source'").fetchone()
    assert source["status"] == "active"


def test_mcp_reingest_preserves_manual_source_edit(monkeypatch):
    manual_key = "wiki/workspace-1/pages/source-page/ops/manual-op"
    manual_uri = h.write_text_object(manual_key + ".md", "# Hand edited source\n\nKept by people.\n")
    h.write_text_object(manual_key + ".json", json.dumps({
        "schema_version": 1, "artifact_type": "manual", "operation_id": "manual-op", "page_id": "source-page",
    }))
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id,page_type,title,slug,summary,markdown_uri,user_id,workspace_id,status,created_at,updated_at) "
            "VALUES ('source-page','source','Source document','doc-1','',%s,'user-1','workspace-1','active',now(),now())",
            (manual_uri,),
        )
        conn.execute(
            "INSERT INTO document_wiki_links(document_id,wiki_page_id,relation_type,workspace_id,created_at) "
            "VALUES ('doc_1','source-page','source_of','workspace-1',now())"
        )
    ledger = [{
        "page_id": "source-page", "ingest_operation_id": "manual-op", "source_document_id": None,
        "sequence_revision": 1, "active": True, "object_key": manual_key + ".json", "lint_operation_id": None,
    }]
    grant, started, finalized = _ingest(monkeypatch, ledger)
    preview = h._intake().preview(started["draft_id"], 1, "workspace-1", "user-1")
    assert not any(item["kind"] == "SOURCE_PAGE" for item in preview["changes"]), "manual body stays as is"

    _apply_through_route(monkeypatch, grant, finalized)
    with h._connect() as conn:
        source = conn.execute("SELECT id,markdown_uri FROM wiki_pages WHERE page_type='source'").fetchone()
    assert source["id"] == "source-page"
    assert h.read_text_object(source["markdown_uri"]) == "# Hand edited source\n\nKept by people.\n"


def test_mcp_reingest_resets_document_quality_review(monkeypatch):
    run_id = str(uuid4())
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs(id,document_id,user_id,workspace_id,input_source,output_dir,mode,status) "
            "VALUES (%s,'doc_1','user-1','workspace-1','doc','out','full','succeeded')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO document_quality_reviews(document_id,workspace_id,ingest_run_id,status,evaluations) "
            "VALUES ('doc_1','workspace-1',%s,'needs_review',%s)",
            (run_id, Jsonb([{"question": "old", "passed": False}])),
        )
    grant, _started, finalized = _ingest(monkeypatch)
    _apply_through_route(monkeypatch, grant, finalized)
    with h._connect() as conn:
        # 옛 편입의 평가 결과는 더 이상 이 문서의 상태가 아니다. MCP 편입은 pipeline run이 없어 평가 전 상태(행 없음)로 둔다.
        assert conn.execute("SELECT count(*) AS n FROM document_quality_reviews").fetchone()["n"] == 0
