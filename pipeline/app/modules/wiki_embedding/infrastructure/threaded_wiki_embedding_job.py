import logging
import threading
import time
from functools import lru_cache

from app.core.pipeline_control import task_run_id

from app.modules.wiki_embedding.application.build_wiki_page_embeddings import (
    BuildWikiPageEmbeddingsUseCase,
    embedding_result,
)
from app.modules.wiki_embedding.application.ports import EmbeddingModelPort
from app.modules.wiki_embedding.infrastructure.bge_m3_embedding_model import (
    BgeM3EmbeddingModel,
)
from app.modules.wiki_embedding.infrastructure.minio_markdown_reader import (
    MinioMarkdownReader,
)
from app.modules.wiki_embedding.infrastructure.postgres_wiki_page_embedding_repository import (
    PostgresWikiPageEmbeddingRepository,
)
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database


_EMBEDDING_LOCK_NAME = "wiki-page-embedding-worker"
_MAX_ATTEMPTS = 3


class ThreadedWikiEmbeddingJob:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def start(self, run_id: str, page_ids: list[str]) -> None:
        if task_run_id.get() is not None:
            # 사용자 작업의 임베딩은 같은 복구 기록에 포함하고 전역 재시도 대상을 섞지 않는다.
            SynchronousWikiEmbeddingJob(self._logger).start(run_id, page_ids)
            return
        thread = threading.Thread(
            target=self._execute,
            args=(run_id, page_ids),
            name=f"wiki-page-embedding-{run_id}",
            daemon=True,
        )
        thread.start()

    def _execute(self, run_id: str, page_ids: list[str]) -> None:
        try:
            repository = PostgresWikiPageEmbeddingRepository()
            model_name = _embedding_model().model_name
            with database.connect() as lock_connection:
                lock_connection.execute(
                    "SELECT pg_advisory_lock(hashtext(%s))",
                    (_EMBEDDING_LOCK_NAME,),
                )
                try:
                    retryable_page_ids = repository.list_retryable_page_ids(model_name)
                    targets = list(dict.fromkeys([*page_ids, *retryable_page_ids]))
                    if not targets:
                        return
                    for attempt in range(1, _MAX_ATTEMPTS + 1):
                        try:
                            result = _build_embeddings(targets)
                            if not result["failed_count"]:
                                self._logger.info(
                                    "wiki page embedding job completed run_id=%s result=%s",
                                    run_id,
                                    result,
                                )
                                return
                            error = f'{result["failed_count"]} page(s) failed'
                        except Exception as exc:
                            error = str(exc)
                        self._logger.warning(
                            "wiki page embedding job retry run_id=%s attempt=%s error=%s",
                            run_id,
                            attempt,
                            error,
                        )
                        if attempt < _MAX_ATTEMPTS:
                            time.sleep(attempt)
                    self._logger.error(
                        "wiki page embedding job failed run_id=%s error=%s",
                        run_id,
                        error,
                    )
                finally:
                    lock_connection.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        (_EMBEDDING_LOCK_NAME,),
                    )
        except Exception as exc:
            self._logger.error(
                "wiki page embedding worker failed run_id=%s error=%s",
                run_id,
                exc,
            )


class DeferredWikiEmbeddingJob:
    def start(self, _run_id: str, _page_ids: list[str]) -> None:
        pass


class SynchronousWikiEmbeddingJob:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def start(self, run_id: str, page_ids: list[str]) -> None:
        if not page_ids:
            return
        result = build_wiki_embeddings(page_ids)
        if result["failed_count"]:
            raise RuntimeError(
                f'wiki restore embedding failed: {result["failed_count"]}'
            )
        self._logger.info(
            "wiki restore embedding completed run_id=%s result=%s",
            run_id,
            result,
        )


def build_wiki_page_embeddings(page_ids: list[str]) -> dict[str, int]:
    return BuildWikiPageEmbeddingsUseCase(
        repository=PostgresWikiPageEmbeddingRepository(),
        embedding_model=_embedding_model(),
        markdown_reader=MinioMarkdownReader(),
    ).execute(page_ids)


def build_wiki_embeddings(page_ids: list[str]) -> dict[str, int]:
    page_result = build_wiki_page_embeddings(page_ids)
    unit_result = build_wiki_unit_embeddings(page_ids)
    return {
        key: page_result[key] + unit_result[key]
        for key in page_result
    }


def _build_embeddings(page_ids: list[str]) -> dict[str, int]:
    return build_wiki_embeddings(page_ids)


def build_wiki_unit_embeddings(
    page_ids: list[str],
    embedding_model: EmbeddingModelPort | None = None,
) -> dict[str, int]:
    unique_page_ids = list(dict.fromkeys(page_ids))
    if not unique_page_ids:
        return embedding_result()

    model = embedding_model or _embedding_model()
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT v.id, v.representation_text, v.status
            FROM wiki_embedding_vectors v
            JOIN wiki_embedding_units u ON u.embedding_vector_id = v.id
            JOIN wiki_pages p ON p.id = u.page_id
            WHERE p.status = 'active'
              AND u.page_id = ANY(%s)
              AND v.embedding_model = %s
            ORDER BY v.id
            """,
            (unique_page_ids, model.model_name),
        ).fetchall()

    pending = [row for row in rows if row["status"] != "completed"]
    if not pending:
        return embedding_result(
            target_count=len(rows),
            skipped_count=len(rows),
        )

    try:
        vectors = model.embed([row["representation_text"] for row in pending])
        if len(vectors) != len(pending):
            raise RuntimeError("embedding model returned an unexpected vector count")
    except Exception as exc:
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE wiki_embedding_vectors
                SET status = 'failed', error = %s, updated_at = now()
                WHERE id = ANY(%s)
                """,
                (str(exc)[:2000], [row["id"] for row in pending]),
            )
        return embedding_result(
            target_count=len(rows),
            skipped_count=len(rows) - len(pending),
            failed_count=len(pending),
        )

    with database.connect() as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                UPDATE wiki_embedding_vectors
                SET embedding_vector = %s,
                    embedding_dimension = %s,
                    status = 'completed',
                    error = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                [
                    (vector, len(vector), row["id"])
                    for row, vector in zip(pending, vectors, strict=True)
                ],
            )
    return embedding_result(
        target_count=len(rows),
        embedded_count=len(pending),
        skipped_count=len(rows) - len(pending),
    )


@lru_cache(maxsize=1)
def _embedding_model() -> BgeM3EmbeddingModel:
    return BgeM3EmbeddingModel()
