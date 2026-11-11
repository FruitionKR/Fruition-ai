"""#264 전 MCP 승인으로 임베딩 없이 발행된 페이지를 일회성 스크립트가 임베딩해 검색 후보에 올린다."""
from app.modules.wiki_embedding.interfaces import backfill_wiki_embeddings as backfill_script
import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401
from test_mcp_web_publication_postgres import _apply_through_route, _ingest
from app.modules.mcp_read.infrastructure.web_wiki_search_candidates import WebWikiSearchCandidates
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch
from app.modules.query.infrastructure.postgres_wiki_repository import PostgresWikiRepository
from app.modules.wiki_embedding.infrastructure import threaded_wiki_embedding_job

MODEL = "backfill-test-model"


class _Model:
    model_name = MODEL

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def input_key(self, text):
        return text


def _dense_scores():
    """MCP search_wiki와 같은 저장 벡터 검색(질문 시점 임베딩 없음)으로 매긴 단위별 점수. 벡터가 없으면 0이다."""
    units = PostgresWikiRepository().list_workspace_embedding_units("workspace-1")
    scores = StoredWikiPageEmbeddingSearch(_Model()).without_live_embedding().score("Alpha", [u.text for u, _ in units])
    return {unit.page_id: score for (unit, _), score in zip(units, scores)}


def _counts():
    with h._connect() as conn:
        return conn.execute(
            "SELECT (SELECT count(*) FROM wiki_page_embeddings) AS pages,"
            " (SELECT count(*) FROM wiki_embedding_vectors WHERE status = 'completed') AS vectors"
        ).fetchone()


def test_backfill_embeds_old_mcp_published_pages_and_rerun_is_noop(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL_NAME", MODEL)
    monkeypatch.setattr(threaded_wiki_embedding_job, "_embedding_model", lambda: _Model())
    # #264 전 상태: 발행은 끝났지만 임베딩 작업이 돌지 않아 페이지 벡터가 없고 단위 벡터는 pending이다.
    grant, _started, finalized = _ingest(monkeypatch)
    _apply_through_route(monkeypatch, grant, finalized)
    with h._connect() as conn:
        active = {str(r["id"]): r["slug"] for r in conn.execute("SELECT id, slug FROM wiki_pages WHERE status='active'")}
    alpha = next(page_id for page_id, slug in active.items() if slug == "alpha")
    assert _counts() == {"pages": 0, "vectors": 0}
    assert _dense_scores()[alpha] == 0

    dry = backfill_script.backfill(model_name=MODEL)
    assert dry["dry_run"] and dry["selected_pages"] == len(active)
    assert _counts() == {"pages": 0, "vectors": 0}, "dry-run은 쓰지 않는다"
    assert backfill_script.backfill(model_name=MODEL, workspace_id="other")["selected_pages"] == 0

    applied = backfill_script.backfill(model_name=MODEL, apply=True, batch_size=1)
    assert applied["selected_pages"] == len(active)
    assert applied["embedded_count"] > 0 and applied["failed_count"] == 0
    assert _counts()["pages"] == len(active)
    assert _dense_scores()[alpha] > 0
    found = WebWikiSearchCandidates(
        StoredWikiPageEmbeddingSearch(_Model()).without_live_embedding(), Bm25Searcher(),
    ).list_candidates(workspace_id="workspace-1", query="Alpha concept")
    assert alpha in found.pages

    rerun = backfill_script.backfill(model_name=MODEL, apply=True)
    assert rerun["selected_pages"] == 0 and rerun["embedded_count"] == 0
