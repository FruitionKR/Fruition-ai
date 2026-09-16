import os
import unittest
from unittest.mock import patch

from app.modules.query.domain.entities import GeneratedAnswer, WikiPage
from app.modules.query.infrastructure.in_memory_wiki_repository import InMemoryWikiRepository
from app.modules.query.interfaces.http import dependencies
from app.modules.query.interfaces.http.dependencies import build_answer_query_use_case


class FixedScoreSearch:
    def __init__(self, score: float) -> None:
        self._score = score

    def score(self, query: str, documents: list[str]) -> list[float]:
        del query
        return [self._score for _ in documents]


class RecordingAnswerGenerator:
    def generate_answer(self, context) -> GeneratedAnswer:
        del context
        return GeneratedAnswer(content="내부 근거에 기반한 답변입니다. [1]")


def _source_page() -> WikiPage:
    return WikiPage(
        id="source:internal",
        page_type="source",
        title="Internal Source",
        slug="internal-source",
        summary="Internal source summary",
        markdown="---\ndocument_id: doc_internal\n---\n\n내부 근거입니다. [B0001]",
    )


def _build_production_use_case(score: float):
    with patch.dict(
        os.environ,
        {"QUERY_EVALUATOR_MODE": "web", "QUERY_EMBEDDING_MODE": "text-only"},
        clear=False,
    ):
        with patch(
            "app.modules.query.interfaces.http.dependencies.Bm25Searcher",
            return_value=FixedScoreSearch(score),
        ), patch(
            "app.modules.query.interfaces.http.dependencies.PostgresWikiRepository",
            return_value=InMemoryWikiRepository([_source_page()], []),
        ), patch(
            "app.modules.query.interfaces.http.dependencies.build_query_chat_answer_generator",
            return_value=RecordingAnswerGenerator(),
        ), patch(
            "app.modules.query.interfaces.http.dependencies.build_query_conversation_summarizer",
            return_value=None,
        ):
            return build_answer_query_use_case(allow_web_search=False)


class QueryHttpDependenciesTest(unittest.TestCase):
    def test_semantic_search_is_reused_across_use_cases(self) -> None:
        dependencies._stored_embedding_search.cache_clear()
        with patch.dict(os.environ, {"QUERY_EMBEDDING_MODE": "bge-m3"}):
            first = dependencies._build_embedding_search(FixedScoreSearch(0.1))
            second = dependencies._build_embedding_search(FixedScoreSearch(0.2))

        self.assertIs(first, second)
        dependencies._stored_embedding_search.cache_clear()

    def test_production_web_disabled_weak_evidence_returns_grounded_no_answer(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUERY_MIN_INTERNAL_RELEVANCE_SCORE", None)
            use_case = _build_production_use_case(0.1)

        self.assertIsNone(use_case._query_evaluator)
        result = use_case.execute(
            "외부 정보가 뭐야?",
            workspace_id="ws_test",
            output_language="ko",
            allow_web_search=False,
        )

        self.assertTrue(result.evidence_snippets)
        self.assertEqual(result.retrieval_summary.stop_reason, "query_evaluator_unsupported")
        self.assertTrue(result.answer.content.startswith("제공된 근거에서 질문에 직접 답할 내용을 찾지 못했습니다."))
        self.assertNotIn("내부 근거에 기반한 답변입니다.", result.answer.content)

    def test_production_internal_evidence_at_default_policy_is_supported(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUERY_MIN_INTERNAL_RELEVANCE_SCORE", None)
            use_case = _build_production_use_case(0.5)

        result = use_case.execute(
            "내부 문서가 뭐야?",
            workspace_id="ws_test",
            output_language="ko",
            allow_web_search=False,
        )

        self.assertEqual(use_case._min_internal_relevance_score, 0.5)
        self.assertIn("내부 근거에 기반한 답변입니다.", result.answer.content)

    def test_production_relevance_policy_can_be_overridden_by_environment(self) -> None:
        with patch.dict(
            os.environ,
            {"QUERY_MIN_INTERNAL_RELEVANCE_SCORE": "0.8"},
            clear=False,
        ):
            use_case = _build_production_use_case(0.5)

        result = use_case.execute(
            "외부 정보가 뭐야?",
            workspace_id="ws_test",
            output_language="ko",
            allow_web_search=False,
        )

        self.assertEqual(use_case._min_internal_relevance_score, 0.8)
        self.assertTrue(result.answer.content.startswith("제공된 근거에서 질문에 직접 답할 내용을 찾지 못했습니다."))
