"""임베딩이 덜 된 활성 위키 페이지를 일회성으로 임베딩한다(PR #264 전 MCP 승인 발행분 보충).

#264 전 MCP 적용은 페이지·단위 행만 쓰고 임베딩 작업을 시작하지 않았다. 대상은 maintenance worker 주기 복구와
같은 조건(list_retryable_page_ids)이다: 완료된 페이지 벡터가 없거나 완료되지 않은 단위 벡터를 가진 활성 페이지.
임베딩 모델만 부르고 같은 표현이 이미 임베딩된 것은 건너뛰어 재실행해도 같다. 배치마다 worker와 같은 advisory lock을
잡아 운영 중에도 임베딩 작업과 겹치지 않는다. 기본은 dry-run이며 --apply로 실행한다. 절차는 docs/script.md 참고.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable

from app.modules.wiki_embedding.infrastructure import threaded_wiki_embedding_job as embedding_job
from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import BgeM3EmbeddingModel
from app.modules.wiki_embedding.infrastructure.postgres_wiki_page_embedding_repository import (
    PostgresWikiPageEmbeddingRepository,
)
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

logger = logging.getLogger("fruition.backfill_wiki_embeddings")


def select_page_ids(model_name: str, workspace_id: str | None = None) -> list[str]:
    page_ids = PostgresWikiPageEmbeddingRepository().list_retryable_page_ids(model_name)
    if workspace_id is None or not page_ids:
        return page_ids
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM wiki_pages WHERE workspace_id = %s AND id = ANY(%s)", (workspace_id, page_ids)
        ).fetchall()
    in_scope = {str(row["id"]) for row in rows}
    return [page_id for page_id in page_ids if page_id in in_scope]


def backfill(
    *,
    workspace_id: str | None = None,
    apply: bool = False,
    batch_size: int = 20,
    model_name: str | None = None,
    build: Callable[[list[str]], dict[str, int]] = embedding_job.build_wiki_embeddings,
) -> dict[str, object]:
    page_ids = select_page_ids(model_name or BgeM3EmbeddingModel().model_name, workspace_id)
    totals = {"target_count": 0, "embedded_count": 0, "skipped_count": 0, "failed_count": 0}
    if apply:
        for start in range(0, len(page_ids), batch_size):
            batch = page_ids[start:start + batch_size]
            with database.connect() as lock:
                lock.execute("SELECT pg_advisory_lock(hashtext(%s))", (embedding_job._EMBEDDING_LOCK_NAME,))
                try:
                    # 인프라 예외는 그대로 올려 멈춘다. 끝난 배치는 커밋돼 있어 다시 실행하면 남은 페이지만 고른다.
                    result = build(batch)
                finally:
                    lock.execute("SELECT pg_advisory_unlock(hashtext(%s))", (embedding_job._EMBEDDING_LOCK_NAME,))
            for key in totals:
                totals[key] += result[key]
            logger.info("batch %s-%s/%s result=%s", start + 1, start + len(batch), len(page_ids), result)
    return {"dry_run": not apply, "workspace_id": workspace_id, "selected_pages": len(page_ids), **totals}


def main() -> None:
    parser = argparse.ArgumentParser(description="임베딩이 덜 된 활성 위키 페이지를 확인하거나 임베딩합니다.")
    parser.add_argument("--workspace-id", help="이 워크스페이스만 처리합니다. 생략하면 전체입니다.")
    parser.add_argument("--batch-size", type=int, default=20, help="한 번에 임베딩할 페이지 수(기본 20)")
    parser.add_argument("--apply", action="store_true", help="지정하면 실제 임베딩합니다. 생략하면 dry-run입니다.")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size는 1 이상이어야 합니다.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = backfill(workspace_id=args.workspace_id, apply=args.apply, batch_size=args.batch_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["failed_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
