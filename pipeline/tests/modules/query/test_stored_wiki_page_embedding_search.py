import hashlib
import unittest
from unittest.mock import patch

from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch


class FakeEmbeddingModel:
    model_name = "test-model"

    def __init__(self) -> None:
        self.embedded_texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded_texts.extend(texts)
        return [[1.0, 0.0] for _ in texts]


class FakeFallbackSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, documents: list[str]) -> list[float]:
        self.calls.append((query, documents))
        return [0.25 for _ in documents]


class FakeConnection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> "FakeCursor":
        self.calls.append((sql, params))
        return FakeCursor(self.rows)


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict]:
        return self.rows


class StoredWikiPageEmbeddingSearchTest(unittest.TestCase):
    def test_exposes_query_embedding_for_global_candidate_search(self) -> None:
        model = FakeEmbeddingModel()
        search = StoredWikiPageEmbeddingSearch(
            embedding_model=model,
            fallback_search=FakeFallbackSearch(),
        )

        query_embedding = search.embed_query("query")
        repeated = search.embed_query("query")

        self.assertEqual(query_embedding.model_name, "test-model")
        self.assertEqual(query_embedding.vector, [1.0, 0.0])
        self.assertIs(repeated, query_embedding)
        self.assertEqual(model.embedded_texts, ["query"])

    def test_scores_with_stored_vectors_without_fallback(self) -> None:
        document = "Stored document"
        document_hash = hashlib.sha256(document.encode("utf-8")).hexdigest()
        fallback = FakeFallbackSearch()
        search = StoredWikiPageEmbeddingSearch(
            embedding_model=FakeEmbeddingModel(),
            fallback_search=fallback,
        )

        with patch(
            "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
            return_value=FakeConnection(
                [
                    {
                        "representation_hash": document_hash,
                        "embedding_vector": [1.0, 0.0],
                    }
                ]
            ),
        ):
            scores = search.score("query", [document])

        self.assertEqual(scores, [1.0])
        self.assertEqual(fallback.calls, [])

    def test_loads_page_and_unit_vectors_with_the_same_model(self) -> None:
        connection = FakeConnection([])
        search = StoredWikiPageEmbeddingSearch(
            embedding_model=FakeEmbeddingModel(),
            fallback_search=FakeFallbackSearch(),
        )

        with patch(
            "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
            return_value=connection,
        ):
            search.score("query", ["document"])

        sql, params = connection.calls[0]
        self.assertIn("FROM wiki_page_embeddings", sql)
        self.assertIn("FROM wiki_embedding_vectors", sql)
        self.assertEqual(params[0], "test-model")
        self.assertEqual(params[2], "test-model")

    def test_falls_back_for_missing_vectors(self) -> None:
        fallback = FakeFallbackSearch()
        search = StoredWikiPageEmbeddingSearch(
            embedding_model=FakeEmbeddingModel(),
            fallback_search=fallback,
        )

        with patch(
            "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
            return_value=FakeConnection([]),
        ):
            scores = search.score("query", ["Missing document"])

        self.assertEqual(scores, [0.25])
        self.assertEqual(fallback.calls, [("query", ["Missing document"])])

    def test_default_fallback_reuses_query_embedding_model(self) -> None:
        embedding_model = FakeEmbeddingModel()
        search = StoredWikiPageEmbeddingSearch(embedding_model=embedding_model)

        with patch(
            "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
            return_value=FakeConnection([]),
        ):
            scores = search.score("query", ["Missing document"])

        self.assertEqual(scores, [1.0])
        self.assertEqual(embedding_model.embedded_texts, ["query", "Missing document"])

    def test_falls_back_for_invalid_stored_vector(self) -> None:
        document = "Broken vector document"
        document_hash = hashlib.sha256(document.encode("utf-8")).hexdigest()
        fallback = FakeFallbackSearch()
        search = StoredWikiPageEmbeddingSearch(
            embedding_model=FakeEmbeddingModel(),
            fallback_search=fallback,
        )

        with patch(
            "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
            return_value=FakeConnection(
                [
                    {
                        "representation_hash": document_hash,
                        "embedding_vector": "-",
                    }
                ]
            ),
        ):
            scores = search.score("query", [document])

        self.assertEqual(scores, [0.25])
        self.assertEqual(fallback.calls, [("query", [document])])

    def test_falls_back_for_non_finite_stored_vector(self) -> None:
        document = "Non-finite vector document"
        document_hash = hashlib.sha256(document.encode("utf-8")).hexdigest()

        for invalid_value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid_value=invalid_value):
                fallback = FakeFallbackSearch()
                search = StoredWikiPageEmbeddingSearch(
                    embedding_model=FakeEmbeddingModel(),
                    fallback_search=fallback,
                )
                with patch(
                    "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
                    return_value=FakeConnection(
                        [
                            {
                                "representation_hash": document_hash,
                                "embedding_vector": [invalid_value, 0.0],
                            }
                        ]
                    ),
                ):
                    scores = search.score("query", [document])

                self.assertEqual(scores, [0.25])
                self.assertEqual(fallback.calls, [("query", [document])])

    def test_clamps_negative_similarity_to_zero(self) -> None:
        document = "Opposite vector document"
        document_hash = hashlib.sha256(document.encode("utf-8")).hexdigest()
        fallback = FakeFallbackSearch()
        search = StoredWikiPageEmbeddingSearch(
            embedding_model=FakeEmbeddingModel(),
            fallback_search=fallback,
        )

        with patch(
            "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
            return_value=FakeConnection(
                [
                    {
                        "representation_hash": document_hash,
                        "embedding_vector": [-1.0, 0.0],
                    }
                ]
            ),
        ):
            scores = search.score("query", [document])

        self.assertEqual(scores, [0.0])
        self.assertEqual(fallback.calls, [])


if __name__ == "__main__":
    unittest.main()
