from unittest.mock import Mock, patch
from datetime import UTC, datetime

import pytest

from app.modules.wiki_embedding.infrastructure.postgres_wiki_page_embedding_repository import (
    PostgresWikiPageEmbeddingRepository,
    reserve_page_embeddings,
)
from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)


@pytest.mark.parametrize("method", ["upsert_embedding", "mark_failed"])
def test_embedding_write_skips_deleted_page(method: str) -> None:
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = None
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    repository = PostgresWikiPageEmbeddingRepository()

    with patch.object(database, "connect", return_value=connection):
        if method == "upsert_embedding":
            repository.upsert_embedding(
                "page-1", "model-1", "hash-1", [0.1], datetime(2026, 8, 10, tzinfo=UTC)
            )
        else:
            repository.mark_failed(
                "page-1", "model-1", "hash-1", "failed", datetime(2026, 8, 10, tzinfo=UTC)
            )

    assert connection.execute.call_count == 1
    assert "FOR UPDATE" in connection.execute.call_args.args[0]


def test_reserve_page_embeddings_marks_unique_active_pages_pending() -> None:
    connection = Mock()
    connection.execute.return_value.rowcount = 2

    reserve_page_embeddings(
        connection,
        ["page-2", "page-1", "page-2"],
        "BAAI/bge-m3",
    )

    query, params = connection.execute.call_args.args
    assert "status = 'active'" in query
    assert "status = 'pending'" in query
    assert params == ("BAAI/bge-m3", ["page-2", "page-1"])


def test_reserve_page_embeddings_rejects_missing_active_page() -> None:
    connection = Mock()
    connection.execute.return_value.rowcount = 1

    with pytest.raises(RuntimeError, match="reserve every wiki page"):
        reserve_page_embeddings(
            connection,
            ["page-1", "deleted-page"],
            "BAAI/bge-m3",
        )


def test_list_retryable_page_ids_returns_pending_and_failed_active_pages() -> None:
    result = Mock()
    result.fetchall.return_value = [{"page_id": "page-1"}, {"page_id": "page-2"}]
    connection = Mock()
    connection.execute.return_value = result
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)

    with patch.object(database, "connect", return_value=connection):
        page_ids = PostgresWikiPageEmbeddingRepository().list_retryable_page_ids(
            "BAAI/bge-m3"
        )

    query, params = connection.execute.call_args.args
    assert "status IN ('pending', 'failed')" in query
    assert "page.status = 'active'" in query
    assert params == ("BAAI/bge-m3",)
    assert page_ids == ["page-1", "page-2"]
