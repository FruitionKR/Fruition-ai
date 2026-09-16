import unittest
from unittest.mock import patch

from app.modules.query.domain.entities import SemanticQueryEmbedding
from app.modules.query.infrastructure.postgres_wiki_repository import PostgresWikiRepository


class FakeCursor:
    def fetchall(self) -> list[dict]:
        return []


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> FakeCursor:
        assert sql.count("%s") == len(params)
        self.calls.append((sql, params))
        return FakeCursor()


class PostgresWikiRepositoryTest(unittest.TestCase):
    def test_concept_only_repository_excludes_source_pages_and_neighbors(self) -> None:
        connection = FakeConnection()

        with patch(
            "app.modules.query.infrastructure.postgres_wiki_repository.database.connect",
            return_value=connection,
        ):
            repository = PostgresWikiRepository(concept_only=True)
            repository.list_candidate_pages(
                "ws_target",
                "검색 질문",
                source_limit=60,
                concept_limit=40,
            )
            repository.list_links_for_page_ids(
                "ws_target",
                ["concept-1"],
                limit=200,
            )
            repository.list_pages_by_ids(
                "ws_target",
                ["concept-2"],
            )

        _candidate_sql, candidate_params = connection.calls[0]
        link_sql, _ = connection.calls[1]
        page_sql, _ = connection.calls[2]
        assert "page_type = 'concept'" in link_sql
        assert "page_type = 'concept'" in page_sql
        assert candidate_params[6] == 0
        assert candidate_params[11] == 0
        assert candidate_params[14] == 0

    def test_active_pages_are_filtered_by_workspace(self) -> None:
        connection = FakeConnection()

        with patch(
            "app.modules.query.infrastructure.postgres_wiki_repository.database.connect",
            return_value=connection,
        ):
            PostgresWikiRepository().list_candidate_pages(
                "ws_target",
                "검색 질문",
                source_limit=60,
                concept_limit=40,
            )

        sql, params = connection.calls[0]
        self.assertIn("p.workspace_id = %s", sql)
        self.assertIn("PARTITION BY p.page_type", sql)
        self.assertIn("type_rank <= %s", sql)
        self.assertLess(
            sql.index("candidate_ids AS"),
            sql.index("string_agg(eu.text"),
        )
        self.assertIn("FROM unit_ranked", sql)
        self.assertIn("@@ websearch_to_tsquery", sql)
        self.assertEqual(
            params,
            (
                "검색 질문",
                "검색 질문",
                "검색 질문",
                "검색 질문",
                "검색 질문",
                "ws_target",
                60,
                40,
                "검색 OR 질문",
                "ws_target",
                "검색 OR 질문",
                60,
                40,
                "검색 질문",
                60,
                40,
            ),
        )

    def test_body_candidates_match_any_query_term(self) -> None:
        connection = FakeConnection()

        with patch(
            "app.modules.query.infrastructure.postgres_wiki_repository.database.connect",
            return_value=connection,
        ):
            PostgresWikiRepository().list_candidate_pages(
                "ws_target",
                "alpha beta",
                source_limit=60,
                concept_limit=40,
            )

        sql, params = connection.calls[0]
        self.assertIn("websearch_to_tsquery('simple', %s)", sql)
        self.assertEqual(params.count("alpha OR beta"), 2)

    def test_global_semantic_candidates_are_queried_independently(self) -> None:
        connection = FakeConnection()

        with patch(
            "app.modules.query.infrastructure.postgres_wiki_repository.database.connect",
            return_value=connection,
        ):
            PostgresWikiRepository().list_candidate_pages(
                "ws_target",
                "표현이 다른 질문",
                source_limit=60,
                concept_limit=40,
                semantic_query=SemanticQueryEmbedding(
                    model_name="test-model",
                    vector=[1.0, 0.0],
                ),
            )

        self.assertEqual(len(connection.calls), 2)
        semantic_sql, semantic_params = connection.calls[1]
        self.assertIn("JOIN wiki_embedding_units", semantic_sql)
        self.assertIn("JOIN wiki_embedding_vectors", semantic_sql)
        self.assertIn("unnest(e.embedding_vector)", semantic_sql)
        self.assertIn("max(vector_score.similarity)", semantic_sql)
        self.assertIn("PARTITION BY page_type", semantic_sql)
        self.assertEqual(
            semantic_params,
            ([1.0, 0.0], "ws_target", "test-model", 2, 60, 40),
        )

    def test_active_links_include_bounded_neighbors_in_workspace(self) -> None:
        connection = FakeConnection()

        with patch(
            "app.modules.query.infrastructure.postgres_wiki_repository.database.connect",
            return_value=connection,
        ):
            PostgresWikiRepository().list_links_for_page_ids(
                "ws_target",
                ["source-1", "concept-1"],
                limit=200,
            )

        sql, params = connection.calls[0]
        self.assertIn("from_page.workspace_id = %s", sql)
        self.assertIn("to_page.workspace_id = %s", sql)
        self.assertIn("l.link_type = ANY(%s)", sql)
        self.assertIn("l.from_page_id = ANY(%s)", sql)
        self.assertIn("l.to_page_id = ANY(%s)", sql)
        self.assertIn("AND NOT (l.to_page_id = ANY(%s))", sql)
        self.assertIn("AND NOT (l.from_page_id = ANY(%s))", sql)
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(
            params,
            (
                "ws_target",
                "ws_target",
                [
                    "child_of",
                    "concept_related_to",
                    "contrasts_with",
                    "part_of",
                    "source_mentions_concept",
                    "supports_or_enables",
                    "uses_or_depends_on",
                ],
                ["source-1", "concept-1"],
                [],
                ["source-1", "concept-1"],
                [],
                200,
            ),
        )

    def test_neighbor_pages_are_loaded_by_bounded_ids(self) -> None:
        connection = FakeConnection()

        with patch(
            "app.modules.query.infrastructure.postgres_wiki_repository.database.connect",
            return_value=connection,
        ):
            PostgresWikiRepository().list_pages_by_ids(
                "ws_target",
                ["concept-neighbor"],
            )

        sql, params = connection.calls[0]
        self.assertIn("workspace_id = %s", sql)
        self.assertIn("id = ANY(%s)", sql)
        self.assertEqual(
            params,
            ("ws_target", ["concept-neighbor"]),
        )


if __name__ == "__main__":
    unittest.main()
