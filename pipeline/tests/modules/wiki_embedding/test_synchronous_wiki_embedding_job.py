import logging
from unittest.mock import Mock, call, patch

import pytest

from app.modules.wiki_embedding.infrastructure import threaded_wiki_embedding_job


def test_restore_embedding_failure_prevents_success() -> None:
    job = threaded_wiki_embedding_job.SynchronousWikiEmbeddingJob(
        logging.getLogger("test")
    )

    with (
        patch.object(
            threaded_wiki_embedding_job,
            "build_wiki_embeddings",
            return_value={"failed_count": 1},
        ) as build_embeddings,
        pytest.raises(RuntimeError, match="embedding failed"),
    ):
        job.start("restore-1", ["page-1"])

    build_embeddings.assert_called_once_with(["page-1"])


def test_threaded_job_recovers_reserved_pages_and_retries_failures() -> None:
    logger = Mock()
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.execute.return_value.fetchone.return_value = {"locked": True}
    repository = Mock()
    repository.list_retryable_page_ids.return_value = ["page-1"]

    with (
        patch.object(threaded_wiki_embedding_job.database, "connect", return_value=connection),
        patch.object(
            threaded_wiki_embedding_job,
            "PostgresWikiPageEmbeddingRepository",
            return_value=repository,
        ),
        patch.object(
            threaded_wiki_embedding_job,
            "_build_embeddings",
            side_effect=[{"failed_count": 1}, {"failed_count": 0}],
        ) as build_embeddings,
        patch.object(threaded_wiki_embedding_job.time, "sleep"),
    ):
        threaded_wiki_embedding_job.ThreadedWikiEmbeddingJob(logger)._execute(
            "pending-recovery",
            [],
        )

    assert build_embeddings.call_args_list == [call(["page-1"]), call(["page-1"])]
    assert "pg_try_advisory_lock" in connection.execute.call_args_list[0].args[0]
    assert "pg_advisory_unlock" in connection.execute.call_args_list[-1].args[0]
    logger.info.assert_called_once()


def test_periodic_recovery_skips_while_another_embedding_job_runs() -> None:
    # 주기 복구는 앞선 작업을 기다리지 않고 건너뛴다. 기다리면 10분마다 스레드가 쌓인다.
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.execute.return_value.fetchone.return_value = {"locked": False}
    repository = Mock()

    with (
        patch.object(threaded_wiki_embedding_job.database, "connect", return_value=connection),
        patch.object(
            threaded_wiki_embedding_job,
            "PostgresWikiPageEmbeddingRepository",
            return_value=repository,
        ),
        patch.object(threaded_wiki_embedding_job, "_embedding_model"),
        patch.object(threaded_wiki_embedding_job, "_build_embeddings") as build_embeddings,
    ):
        threaded_wiki_embedding_job.ThreadedWikiEmbeddingJob(Mock())._execute("pending-recovery", [])

    build_embeddings.assert_not_called()
    repository.list_retryable_page_ids.assert_not_called()
    assert len(connection.execute.call_args_list) == 1
