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


def _lock_connection() -> Mock:
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    return connection


def test_recover_waits_for_running_job_and_raises_after_infrastructure_retries() -> None:
    # MCP 발행 깨우기는 앞선 복구를 건너뛰지 않고 기다리며, 인프라 실패가 끝내 이어지면 command를 완료 처리하지 않게 예외를 올린다.
    connection = _lock_connection()
    repository = Mock()
    repository.list_retryable_page_ids.return_value = ["page-1"]

    with (
        patch.object(threaded_wiki_embedding_job.database, "connect", return_value=connection),
        patch.object(threaded_wiki_embedding_job, "PostgresWikiPageEmbeddingRepository", return_value=repository),
        patch.object(threaded_wiki_embedding_job, "_embedding_model"),
        patch.object(threaded_wiki_embedding_job, "_build_embeddings", side_effect=RuntimeError("db down")) as build,
        patch.object(threaded_wiki_embedding_job.time, "sleep"),
        pytest.raises(RuntimeError, match="recovery failed: db down"),
    ):
        threaded_wiki_embedding_job.ThreadedWikiEmbeddingJob(Mock()).recover("mcp-index:event-1")

    assert build.call_count == threaded_wiki_embedding_job._MAX_ATTEMPTS
    assert "SELECT pg_advisory_lock" in connection.execute.call_args_list[0].args[0]
    assert "pg_advisory_unlock" in connection.execute.call_args_list[-1].args[0]


def test_recover_returns_after_permanent_page_failure_once_healthy_pages_are_stored() -> None:
    # Markdown이 없는 페이지 하나가 있어도 같은 복구의 정상 페이지는 저장되고, 깨우기는 예외 없이 끝나 다음 command로 넘어간다.
    # 실패 페이지는 failed로 기록되어 다음 복구의 대상 목록(완료 행 없음)에 계속 남는다.
    from datetime import datetime

    from app.modules.wiki_embedding.application.build_wiki_page_embeddings import BuildWikiPageEmbeddingsUseCase
    from app.modules.wiki_embedding.domain.entities import WikiPageEmbeddingTarget

    updated_at = datetime(2026, 11, 8)
    repository = Mock()
    repository.list_retryable_page_ids.return_value = ["healthy", "missing"]
    repository.list_active_pages_by_ids.return_value = [
        WikiPageEmbeddingTarget("healthy", "정상", "", "s3://wiki/healthy.md", updated_at),
        WikiPageEmbeddingTarget("missing", "누락", "", "s3://wiki/missing.md", updated_at),
    ]
    stored = {}
    repository.existing_hashes.side_effect = lambda *_args: dict(stored)
    repository.upsert_embedding.side_effect = lambda page_id, _model, digest, *_args: stored.__setitem__(page_id, digest)
    reader = Mock()
    reader.read_markdown.side_effect = lambda uri: (_ for _ in ()).throw(FileNotFoundError(uri)) if "missing" in uri else "본문"
    model = Mock(model_name="bge-m3")
    model.embed.side_effect = lambda texts: [[0.1] for _ in texts]
    logger = Mock()

    def build(page_ids):
        return BuildWikiPageEmbeddingsUseCase(repository, model, reader).execute(page_ids)

    with (
        patch.object(threaded_wiki_embedding_job.database, "connect", return_value=_lock_connection()),
        patch.object(threaded_wiki_embedding_job, "PostgresWikiPageEmbeddingRepository", return_value=repository),
        patch.object(threaded_wiki_embedding_job, "_embedding_model", return_value=model),
        patch.object(threaded_wiki_embedding_job, "_build_embeddings", side_effect=build) as build_embeddings,
        patch.object(threaded_wiki_embedding_job.time, "sleep"),
    ):
        threaded_wiki_embedding_job.ThreadedWikiEmbeddingJob(logger).recover("mcp-index:event-1")

    assert build_embeddings.call_count == threaded_wiki_embedding_job._MAX_ATTEMPTS
    assert repository.upsert_embedding.call_count == 1
    assert list(stored) == ["healthy"]
    assert {c.args[0] for c in repository.mark_failed.call_args_list} == {"missing"}
    # 성공으로 기록하지 않는다.
    logger.info.assert_not_called()
    logger.error.assert_called_once()
