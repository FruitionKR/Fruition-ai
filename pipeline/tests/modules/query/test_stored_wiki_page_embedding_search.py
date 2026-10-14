import hashlib
import unittest
from unittest.mock import patch

import httpx

from app.modules.query.infrastructure.stored_wiki_page_embedding_search import StoredWikiPageEmbeddingSearch


class FakeEmbeddingModel:
    model_name = "test-model"

    def __init__(self) -> None:
        self.embedded_texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded_texts.extend(texts)
        return [[1.0, 0.0] for _ in texts]


class FailingDocumentEmbeddingModel(FakeEmbeddingModel):
    """질문 임베딩은 되지만 문서 묶음 임베딩은 임베딩 서버 타임아웃·연결 실패로 끝난다."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def embed(self, texts: list[str]) -> list[list[float]]:
        if texts != ["query"]:
            raise self.error
        return super().embed(texts)


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

    def test_without_live_embedding_scores_missing_vectors_as_zero(self) -> None:
        model = FakeEmbeddingModel()
        base = StoredWikiPageEmbeddingSearch(embedding_model=model)
        search = base.without_live_embedding()

        with patch(
            "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
            return_value=FakeConnection([]),
        ):
            scores = search.score("query", ["저장 벡터 없는 unit"])

        # 벡터가 없는 문서는 임베딩하지 않고 0점이며, 질문 임베딩만 계산한다.
        self.assertEqual(scores, [0.0])
        self.assertEqual(model.embedded_texts, ["query"])
        self.assertIs(search._cached_embed_query, base._cached_embed_query)

    def test_online_text_limit_embeds_only_short_missing_documents(self) -> None:
        model = FakeEmbeddingModel()
        search = StoredWikiPageEmbeddingSearch(embedding_model=model, online_text_limit=10)

        with patch(
            "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
            return_value=FakeConnection([]),
        ):
            scores = search.score("query", ["짧은 섹션", "x" * 11])

        # 짧은 문서는 질문 시점에 임베딩하고, 상한을 넘는 문서는 임베딩하지 않아 0점이다.
        self.assertEqual(scores, [1.0, 0.0])
        self.assertEqual(model.embedded_texts, ["query", "짧은 섹션"])

    def test_online_embedding_failure_scores_only_stored_vectors(self) -> None:
        stored = "저장 벡터가 있는 페이지"
        stored_hash = hashlib.sha256(stored.encode("utf-8")).hexdigest()
        for error in (httpx.ReadTimeout("slow"), httpx.ConnectError("down")):
            search = StoredWikiPageEmbeddingSearch(embedding_model=FailingDocumentEmbeddingModel(error))

            with patch(
                "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
                return_value=FakeConnection([{"representation_hash": stored_hash, "embedding_vector": [1.0, 0.0]}]),
            ), self.assertLogs(
                "app.modules.query.infrastructure.stored_wiki_page_embedding_search", level="WARNING"
            ) as logs:
                scores = search.score("query", [stored, "저장 벡터가 없는 페이지"])

            # 질의는 실패하지 않고, 저장 벡터가 있는 페이지만 임베딩 점수를 받는다.
            self.assertEqual(scores, [1.0, 0.0])
            self.assertIn("Online embedding failed", logs.output[0])

    def test_online_embedding_failure_without_any_stored_vector_scores_zero(self) -> None:
        search = StoredWikiPageEmbeddingSearch(embedding_model=FailingDocumentEmbeddingModel(httpx.ReadTimeout("slow")))

        with patch(
            "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
            return_value=FakeConnection([]),
        ), self.assertLogs("app.modules.query.infrastructure.stored_wiki_page_embedding_search", level="WARNING"):
            scores = search.score("query", ["a", "b"])

        self.assertEqual(scores, [0.0, 0.0])

    def test_online_embedding_does_not_hide_non_transport_errors(self) -> None:
        search = StoredWikiPageEmbeddingSearch(
            embedding_model=FailingDocumentEmbeddingModel(RuntimeError("model mismatch"))
        )

        with patch(
            "app.modules.query.infrastructure.stored_wiki_page_embedding_search.database.connect",
            return_value=FakeConnection([]),
        ), self.assertRaisesRegex(RuntimeError, "model mismatch"):
            search.score("query", ["a"])


if __name__ == "__main__":
    unittest.main()
