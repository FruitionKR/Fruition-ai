"""편입 후보 비교 벡터. 작업자의 메모리 캐시가 비어도 동일 입력은 DB에서 재사용한다."""
import hashlib
import logging

import psycopg

from app.core.ai_database import connect_ai
from app.core.pipeline_control import task_run_id
from app.modules.wiki_embedding.application.ports import EmbeddingModelPort

logger = logging.getLogger(__name__)
CACHE_LIMIT = 20_000


def embed_cached(model: EmbeddingModelPort, texts: list[str]) -> list[list[float]]:
    run_id = task_run_id.get()
    if not run_id or not texts:
        return model.embed(texts)
    hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
    workspace_id = None
    found = {}
    try:
        with connect_ai() as conn:
            task = conn.execute("SELECT workspace_id FROM ai_task_runs WHERE id = %s", (run_id,)).fetchone()
            if task:
                workspace_id = task["workspace_id"]
                rows = conn.execute(
                    "SELECT representation_hash, embedding_vector FROM concept_embedding_cache "
                    "WHERE workspace_id = %s AND embedding_model = %s AND representation_hash = ANY(%s)",
                    (workspace_id, model.model_name, hashes),
                ).fetchall()
                found = {row["representation_hash"]: row["embedding_vector"] for row in rows}
    except psycopg.Error:
        logger.warning("[개념 임베딩 DB 캐시] 읽기 실패, 모델로 계산", exc_info=True)
    missing = {key: text for key, text in zip(hashes, texts, strict=True) if key not in found}
    logger.info("[개념 임베딩 DB 캐시] inputs=%d hits=%d misses=%d", len(hashes), len(found), len(missing))
    if missing:
        computed = dict(zip(missing, model.embed(list(missing.values())), strict=True))
        if workspace_id:
            try:
                with connect_ai() as conn:
                    with conn.cursor() as cursor:
                        cursor.executemany(
                            "INSERT INTO concept_embedding_cache "
                            "(workspace_id, embedding_model, representation_hash, embedding_vector, owner_run_id) "
                            "SELECT workspace_id, %s, %s, %s, id FROM ai_task_runs "
                            "WHERE id = %s AND workspace_id = %s AND NOT EXISTS "
                            "(SELECT 1 FROM ai_purged_scopes WHERE scope_type = 'workspace' AND scope_id = %s) "
                            "ON CONFLICT (workspace_id, embedding_model, representation_hash) DO NOTHING",
                            [(model.model_name, key, vector, run_id, workspace_id, workspace_id)
                             for key, vector in computed.items()],
                        )
                    # 원문은 저장하지 않으며, 과거 표현은 워크스페이스별 최근 2만 개까지만 보관한다.
                    conn.execute(
                        "DELETE FROM concept_embedding_cache WHERE workspace_id = %s AND (embedding_model, representation_hash) IN "
                        "(SELECT embedding_model, representation_hash FROM concept_embedding_cache WHERE workspace_id = %s "
                        "ORDER BY created_at DESC, embedding_model, representation_hash OFFSET %s)",
                        (workspace_id, workspace_id, CACHE_LIMIT),
                    )
            except psycopg.Error:
                logger.warning("[개념 임베딩 DB 캐시] 저장 실패, 계산 결과 사용", exc_info=True)
        found.update(computed)
    return [found[key] for key in hashes]
