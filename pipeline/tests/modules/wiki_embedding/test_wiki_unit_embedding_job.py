from unittest.mock import patch

from app.modules.wiki_embedding.infrastructure.threaded_wiki_embedding_job import (
    build_wiki_unit_embeddings,
)


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return self._rows


class FakeConnection:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.updates: list[tuple[str, tuple]] = []
        self.update_batches: list[tuple[str, list[tuple]]] = []

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> FakeCursor:
        if sql.lstrip().startswith("SELECT"):
            return FakeCursor(self._rows)
        self.updates.append((sql, params))
        return FakeCursor([])

    def cursor(self) -> "FakeConnection":
        return self

    def executemany(self, sql: str, params: list[tuple]) -> None:
        self.update_batches.append((sql, params))


class FakeEmbeddingModel:
    model_name = "test-model"

    def embed(self, texts: list[str]) -> list[list[float]]:
        assert texts == ["첫 근거"]
        return [[1.0, 0.0]]


def test_build_wiki_unit_embeddings_persists_pending_vectors() -> None:
    connection = FakeConnection(
        [
            {"id": "vector-1", "representation_text": "첫 근거", "status": "pending"},
            {"id": "vector-2", "representation_text": "기존 근거", "status": "completed"},
        ]
    )

    with patch(
        "app.modules.wiki_embedding.infrastructure.threaded_wiki_embedding_job.database.connect",
        return_value=connection,
    ):
        result = build_wiki_unit_embeddings(
            ["page-1", "page-1"],
            embedding_model=FakeEmbeddingModel(),
        )

    assert result == {
        "target_count": 2,
        "embedded_count": 1,
        "skipped_count": 1,
        "failed_count": 0,
    }
    assert connection.updates == []
    assert connection.update_batches[0][1] == [([1.0, 0.0], 2, "vector-1")]
