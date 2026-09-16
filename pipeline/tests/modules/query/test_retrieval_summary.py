import unittest

from app.modules.query.application.retrieval_summary import build_retrieval_summary
from app.modules.query.domain.entities import RetrievedPage, WikiPage


def page(page_id: str, page_type: str) -> WikiPage:
    return WikiPage(
        id=page_id,
        page_type=page_type,
        title=page_id,
        slug=page_id,
        summary=f"{page_id} summary",
    )


class RetrievalSummaryTest(unittest.TestCase):
    def test_builds_counts_from_related_pages(self) -> None:
        summary = build_retrieval_summary(
            related_pages=[
                RetrievedPage(page=page("source:a", "source"), score=0.9, role="seed", depth=0),
                RetrievedPage(page=page("concept:a", "concept"), score=0.8, role="focus_concept", depth=2),
                RetrievedPage(page=page("web:a", "web"), score=0.7, role="web_search_result", depth=1),
            ],
            source_candidate_count=3,
            concept_candidate_count=2,
            stop_reason="internal_web_augmented",
        )

        self.assertEqual(summary.source_candidate_count, 3)
        self.assertEqual(summary.concept_candidate_count, 2)
        self.assertEqual(summary.visited_node_count, 3)
        self.assertEqual(summary.returned_node_count, 3)
        self.assertEqual(summary.used_source_count, 1)
        self.assertEqual(summary.used_concept_count, 1)
        self.assertEqual(summary.max_depth, 2)
        self.assertEqual(summary.stop_reason, "internal_web_augmented")


if __name__ == "__main__":
    unittest.main()
