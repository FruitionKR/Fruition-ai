"""MCP 승인 적용은 웹 편입 반영 코드를 그대로 탄다: 임베딩·휴지통 해제·수동 본문 보존·품질 상태 초기화."""
import asyncio
import json
import logging
from uuid import uuid4

from psycopg.types.json import Jsonb

import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401
from app.modules.mcp_read.infrastructure.web_wiki_search_candidates import WebWikiSearchCandidates
from app.modules.mcp_wiki_draft.infrastructure import apply_repository
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch
from app.modules.mcp_wiki_draft.interfaces.http import apply_routes
from app.modules.wiki_embedding.infrastructure import bge_m3_embedding_model, threaded_wiki_embedding_job


class _Model:
    model_name = "test-model"

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def input_key(self, text):
        return text


def _no_local_model(*_args):
    raise AssertionError("pipeline-api must not load the local BGE-M3 model")


def _apply_through_route(monkeypatch, grant, finalized):
    """운영 라우트의 조립(get_mcp_wiki_draft_apply)으로 적용한다. 승인 조회만 테스트 대역으로 바꾼다.
    pipeline-api 안에서는 로컬 BGE-M3를 올리지 않는다(올리면 실패한다)."""
    monkeypatch.setattr(bge_m3_embedding_model, "_load_sentence_transformer", _no_local_model)
    monkeypatch.setattr(threaded_wiki_embedding_job, "_embedding_model", _no_local_model)
    monkeypatch.setattr(apply_routes, "ConfiguredWikiApprovalAuthority", h._ValidApprovalAuthority)
    apply_routes.get_mcp_wiki_draft_apply.cache_clear()
    try:
        payload = h._apply_payload(grant, finalized)
        use_case = apply_routes.get_mcp_wiki_draft_apply()
        # 웹 편입 worker(get_deferred_pipeline_run_use_case)와 같은 지연 임베딩 작업을 쓴다.
        assert type(use_case._repository._embedding_job) is threaded_wiki_embedding_job.DeferredWikiEmbeddingJob
        result = use_case.apply(payload.draft_id, payload)
    finally:
        apply_routes.get_mcp_wiki_draft_apply.cache_clear()
    assert result["state"] == "PUBLISHED"
    return payload


def _ingest(monkeypatch, ledger=()):
    monkeypatch.setattr(h.calculation, "read_contributions", lambda *_args: list(ledger))
    grant = str(uuid4())
    intake = h._intake()
    started = h._complete_intake(intake, grant)
    finalized = intake.finalize(started["run_id"], h._finalize_payload(grant, started))
    return grant, started, finalized


def test_mcp_apply_defers_embedding_to_maintenance_worker_like_web_ingest(monkeypatch):
    grant, _started, finalized = _ingest(monkeypatch)
    payload = _apply_through_route(monkeypatch, grant, finalized)

    with h._connect() as conn:
        active = {
            str(row["id"]): row for row in conn.execute(
                "SELECT id,page_type,slug FROM wiki_pages WHERE status='active'"
            ).fetchall()
        }
        # 웹 편입 worker처럼 API는 발행만 하고 임베딩을 만들지 않는다.
        assert conn.execute(
            "SELECT count(*) AS n FROM wiki_page_embeddings WHERE status='completed'"
        ).fetchone()["n"] == 0
    assert active

    # 발행 결과가 Document로 간다(Document는 이를 받아 embedding_recovery로 maintenance worker를 깨운다).
    sent = []
    monkeypatch.setattr(apply_repository, "_send_publication_receipt", sent.append)
    assert asyncio.run(apply_repository.relay_pending_mcp_publications()) == 1
    assert [(event["operation_id"], event["changed"]) for event in sent] == [(payload.operation_id, True)]
    with h._connect() as conn:
        assert conn.execute("SELECT state FROM mcp_wiki_publication_outbox").fetchone()["state"] == "sent"

    # maintenance worker의 embedding_recovery 처리(recover)가 웹 post_ingest와 같은 build_wiki_embeddings로 임베딩한다.
    monkeypatch.setattr(threaded_wiki_embedding_job, "_embedding_model", lambda: _Model())
    threaded_wiki_embedding_job.ThreadedWikiEmbeddingJob(logging.getLogger("test")).recover("mcp-index:test")
    with h._connect() as conn:
        embedded = {
            str(row["page_id"]) for row in conn.execute(
                "SELECT page_id FROM wiki_page_embeddings WHERE status='completed' AND embedding_model='test-model'"
            ).fetchall()
        }
    assert embedded == set(active)

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


def test_web_manual_edit_is_embedded_by_maintenance_recovery_not_api(monkeypatch):
    """웹 직접 편집 라우트도 pipeline-api에서 발행만 하고, embedding_recovery로 깨운 worker의 recover가 임베딩한다."""
    from app.modules.wiki_ingestion.interfaces.http import routes
    from app.modules.wiki_ingestion.interfaces.http.schemas import WikiPageManualEditIn

    monkeypatch.setenv("EMBEDDING_MODEL_NAME", _Model.model_name)
    grant, _started, finalized = _ingest(monkeypatch)
    _apply_through_route(monkeypatch, grant, finalized)
    monkeypatch.setattr(threaded_wiki_embedding_job, "_embedding_model", lambda: _Model())
    job = threaded_wiki_embedding_job.ThreadedWikiEmbeddingJob(logging.getLogger("test"))
    job.recover("embedding-recovery:apply")
    with h._connect() as conn:
        alpha = conn.execute("SELECT id FROM wiki_pages WHERE slug='alpha' AND status='active'").fetchone()["id"]
        before = conn.execute(
            "SELECT representation_hash FROM wiki_page_embeddings WHERE page_id=%s AND status='completed'", (alpha,)
        ).fetchone()["representation_hash"]

    monkeypatch.setattr(bge_m3_embedding_model, "_load_sentence_transformer", _no_local_model)
    monkeypatch.setattr(threaded_wiki_embedding_job, "_embedding_model", _no_local_model)
    edit = WikiPageManualEditIn(
        user_id="user-1", workspace_id="workspace-1", operation_id="manual-op-1",
        markdown="# Alpha\n\nHand edited alpha body about rollouts.\n",
    )
    assert routes.save_manual_wiki_edit(alpha, edit)["page_id"] == alpha
    with h._connect() as conn:
        reserved = conn.execute(
            "SELECT status FROM wiki_page_embeddings WHERE page_id=%s", (alpha,)
        ).fetchone()["status"]
    # API는 편집한 페이지를 임베딩하지 않고 웹 lint처럼 미완료로 예약만 한다.
    assert reserved == "pending"

    monkeypatch.setattr(threaded_wiki_embedding_job, "_embedding_model", lambda: _Model())
    job.recover("embedding-recovery:manual-op-1")
    with h._connect() as conn:
        after = conn.execute(
            "SELECT representation_hash FROM wiki_page_embeddings WHERE page_id=%s AND status='completed'", (alpha,)
        ).fetchone()["representation_hash"]
    assert after != before


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
