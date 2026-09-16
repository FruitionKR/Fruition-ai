import os
import unittest
from unittest.mock import Mock, patch

from app.modules.query.domain.entities import QueryEvaluation
from app.modules.query.infrastructure.query_evaluator_studio_graph import graph


class QueryEvaluatorStudioGraphTest(unittest.TestCase):
    def test_studio_graph_propagates_web_search_availability(self) -> None:
        with patch(
            "app.modules.query.infrastructure.query_evaluator_studio_graph.build_query_answer_evaluator",
            return_value=None,
        ) as build_evaluator:
            result = graph.invoke(
                {
                    "question": "LangGraph Studio에서 evaluator graph를 볼 수 있나요?",
                    "answer": "Studio용 query_evaluator graph로 확인할 수 있습니다. [1]",
                    "web_search_available": True,
                    "max_attempts": 1,
                    "evidence_snippets": [
                        {
                            "rank": 1,
                            "source_document_id": "doc",
                            "source_block_ids": ["B0001"],
                            "text": "query_evaluator graph는 langgraph.json에 등록되어 Studio에서 확인할 수 있다.",
                        }
                    ],
                }
            )

        build_evaluator.assert_called_once_with(
            provider="openai",
            model="gpt-5-nano",
            web_search_available=True,
        )
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(result["evaluation"]["route"], "internal_supported")
        self.assertIn("reason", result["evaluation"])

    def test_studio_graph_passes_web_search_availability_to_evaluator(self) -> None:
        evaluator = Mock()
        evaluator.evaluate.return_value = QueryEvaluation(route="internal_supported")
        with patch(
            "app.modules.query.infrastructure.query_evaluator_studio_graph.build_query_answer_evaluator",
            return_value=evaluator,
        ) as build_evaluator:
            graph.invoke(
                {
                    "question": "질문",
                    "answer": "답변",
                    "web_search_available": True,
                }
            )

        build_evaluator.assert_called_once_with(
            provider="openai",
            model="gpt-5-nano",
            web_search_available=True,
        )
        self.assertTrue(evaluator.evaluate.call_args.kwargs["web_search_available"])

    def test_studio_graph_falls_back_without_openai_key_in_llm_mode(self) -> None:
        with patch.dict(os.environ, {"QUERY_EVALUATOR_MODE": "llm"}):
            os.environ.pop("OPENAI_API_KEY", None)
            result = graph.invoke({"question": "질문", "answer": "답변"})

        self.assertEqual(result["evaluation"]["route"], "internal_supported")
        self.assertIn("API key", result["evaluation"]["reason"])


if __name__ == "__main__":
    unittest.main()
