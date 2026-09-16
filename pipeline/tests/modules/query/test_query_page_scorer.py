import unittest

from app.modules.query.application.query_page_scorer import QueryPageScorer
from app.modules.query.domain.entities import QueryRewrite, WikiPage


class ScoreSearch:
    def __init__(self, scores_by_document: dict[str, float]) -> None:
        self._scores_by_document = scores_by_document

    def score(self, query: str, documents: list[str]) -> list[float]:
        return [self._scores_by_document.get(document, 0.0) for document in documents]


class EmptyTextSearch:
    def score(self, query: str, documents: list[str]) -> list[float]:
        return [0.0 for _ in documents]


def source_page(page_id: str, title: str, markdown: str = "") -> WikiPage:
    return WikiPage(
        id=page_id,
        page_type="source",
        title=title,
        slug=title.lower().replace(" ", "-"),
        summary=f"{title} summary",
        markdown=markdown,
    )


def concept_page(page_id: str, title: str, markdown: str = "") -> WikiPage:
    return WikiPage(
        id=page_id,
        page_type="concept",
        title=title,
        slug=title.lower().replace(" ", "-"),
        summary=f"{title} summary",
        markdown=markdown,
    )


class QueryPageScorerTest(unittest.TestCase):
    def test_semantic_only_concept_can_pass_recalibrated_focus_threshold(self) -> None:
        page = concept_page("concept:semantic", "Semantic Concept")
        representation = "\n".join([page.title, page.summary])
        scorer = QueryPageScorer(
            embedding_search=ScoreSearch({representation: 0.75}),
            text_search=EmptyTextSearch(),
        )

        scores = scorer.score_pages(
            QueryRewrite(
                original_question="표현이 다른 질문",
                retrieval_query="표현이 다른 질문",
            ),
            [page],
            embedding_weight=0.6,
        )

        self.assertEqual(
            scorer.select_focus_concepts([page], scores),
            ["concept:semantic"],
        )

    def test_selects_all_seed_sources_near_top_score(self) -> None:
        pages = [
            source_page("source:first", "First Source"),
            source_page("source:second", "Second Source"),
            source_page("source:third", "Third Source"),
            source_page("source:fourth", "Fourth Source"),
            source_page("source:low", "Low Source"),
        ]
        scorer = QueryPageScorer(
            embedding_search=EmptyTextSearch(),
            text_search=EmptyTextSearch(),
            source_candidate_limit=10,
        )

        selected = scorer.select_seed_sources(
            pages,
            {
                "source:first": 1.0,
                "source:second": 0.99,
                "source:third": 0.98,
                "source:fourth": 0.981,
                "source:low": 0.80,
            },
        )

        self.assertEqual(selected, ["source:first", "source:second", "source:fourth", "source:third"])

    def test_scores_source_structure_sections_independently(self) -> None:
        markdown = "\n".join(
            [
                "## Categories",
                "general",
                "",
                "## Section Candidates",
                "motor design variables",
            ]
        )
        page = source_page("source:motor", "Motor Paper", markdown)
        representation = "\n".join([page.title, page.summary, markdown])
        scorer = QueryPageScorer(
            embedding_search=ScoreSearch(
                {
                    representation: 0.10,
                    "Section Candidates\nmotor design variables": 1.0,
                }
            ),
            text_search=EmptyTextSearch(),
        )

        scores = scorer.score_pages(
            QueryRewrite(
                original_question="design variables",
                retrieval_query="design variables",
            ),
            [page],
            embedding_weight=1.0,
        )

        self.assertAlmostEqual(scores["source:motor"], 0.35)

    def test_can_skip_source_structure_rescoring_for_stored_unit_vectors(self) -> None:
        page = source_page(
            "source:motor",
            "Motor Paper",
            "## Section Candidates\nmotor design variables",
        )
        representation = "\n".join([page.title, page.summary, page.markdown])
        search = ScoreSearch({representation: 0.10})
        scorer = QueryPageScorer(
            embedding_search=search,
            text_search=EmptyTextSearch(),
            score_source_structures=False,
        )

        scores = scorer.score_pages(
            QueryRewrite(
                original_question="design variables",
                retrieval_query="design variables",
            ),
            [page],
            embedding_weight=1.0,
        )

        self.assertAlmostEqual(scores["source:motor"], 0.10)

    def test_selects_direct_concept_match_from_alias(self) -> None:
        page = concept_page(
            "concept:persistent-wiki",
            "Persistent Wiki",
            "aliases:\n- 지속적 위키",
        )
        scorer = QueryPageScorer(
            embedding_search=EmptyTextSearch(),
            text_search=EmptyTextSearch(),
        )

        selected = scorer.select_direct_match_concepts(
            QueryRewrite(
                original_question="지속적위키가 뭐야?",
                retrieval_query="지속적위키가 뭐야?",
            ),
            [page],
        )

        self.assertEqual(selected, ["concept:persistent-wiki"])

    def test_name_matching_does_not_partially_strip_short_compound_particle(self) -> None:
        scorer = QueryPageScorer(embedding_search=EmptyTextSearch(), text_search=EmptyTextSearch())
        query = QueryRewrite(
            original_question="나에서는 뭐야?",
            retrieval_query="나에서는",
            keywords=["나에서는"],
        )

        self.assertEqual(scorer._normalize_name("나에서는"), "나에서는")
        self.assertEqual(scorer._normalize_name("문서에서는"), "문서")
        self.assertEqual(
            scorer._name_match_score(query, concept_page("concept:naeseo", "나서")),
            0.0,
        )
        self.assertEqual(
            scorer._name_match_score(
                QueryRewrite(
                    original_question="문서에서는 뭐야?",
                    retrieval_query="문서",
                    keywords=["문서"],
                ),
                concept_page("concept:document", "문서"),
            ),
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
