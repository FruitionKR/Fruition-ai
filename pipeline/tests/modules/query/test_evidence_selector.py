import unittest

from app.modules.query.application.build_query_context import BuildQueryContextUseCase
from app.modules.query.application.evidence_selector import EvidenceSelector
from app.modules.query.domain.entities import GraphContext, RetrievedPage, WikiEmbeddingUnit, WikiPage


class EmptySearch:
    def score(self, query: str, documents: list[str]) -> list[float]:
        return [0.0 for _ in documents]


class QueryContainsSearch:
    def score(self, query: str, documents: list[str]) -> list[float]:
        query_terms = [term.lower() for term in query.split() if term.strip()]
        scores = []
        for document in documents:
            lowered = document.lower()
            scores.append(1.0 if any(term in lowered for term in query_terms) else 0.0)
        return scores


class EvidenceSelectorTest(unittest.TestCase):
    def test_keeps_selected_source_title_and_content_in_answer_context(self) -> None:
        source_title = "Exact Source Title"
        source_content = "Exact source content must remain available"
        page = WikiPage(
            id="source:exact",
            page_type="source",
            title=source_title,
            slug="exact-source",
            summary="Source summary",
            markdown=f"---\ndocument_id: doc_exact\n---\n\n{source_content}. [B0001]",
        )

        context = BuildQueryContextUseCase().execute(
            question="source content",
            related_pages=[RetrievedPage(page=page, score=0.9, role="seed_source")],
            graph_context=GraphContext(),
            traversal_paths=[],
        )

        self.assertEqual(context.evidence_snippets[0].text, f"{source_content}.")
        self.assertIn(source_title, context.answer_context)
        self.assertIn(source_content, context.answer_context)

    def test_selects_only_text_with_source_block_refs(self) -> None:
        page = WikiPage(
            id="source:seed",
            page_type="source",
            title="Seed Source",
            slug="seed-source",
            summary="Seed summary",
            markdown=(
                "---\ndocument_id: doc_seed\n---\n\n"
                "블록 참조가 없는 설명입니다.\n\n"
                "원본에 연결된 근거 문장입니다. [B0005]"
            ),
        )
        selector = EvidenceSelector(
            embedding_search=QueryContainsSearch(),
            text_search=EmptySearch(),
        )

        snippets = selector.select(
            question="근거 문장",
            related_pages=[
                RetrievedPage(
                    page=page,
                    score=0.9,
                    role="seed_source",
                )
            ],
            embedding_units_by_page_id={},
        )

        self.assertEqual(len(snippets), 1)
        self.assertEqual(snippets[0].source_document_id, "doc_seed")
        self.assertEqual(snippets[0].source_block_ids, ["B0005"])
        self.assertEqual(snippets[0].source_refs[0].source_document_id, "doc_seed")
        self.assertEqual(snippets[0].source_refs[0].source_block_id, "B0005")
        self.assertEqual(snippets[0].text, "원본에 연결된 근거 문장입니다.")

    def test_maps_global_source_refs_to_structured_refs(self) -> None:
        page = WikiPage(
            id="concept:shared",
            page_type="concept",
            title="Shared Concept",
            slug="shared-concept",
            summary="Shared summary",
            markdown=(
                "---\nsources: doc_a, doc_b\n---\n\n"
                "- 여러 원문을 함께 참조하는 근거입니다. [doc_a:B0001, doc_b:B0008]"
            ),
        )
        selector = EvidenceSelector(
            embedding_search=QueryContainsSearch(),
            text_search=EmptySearch(),
        )

        snippets = selector.select(
            question="여러 원문 근거",
            related_pages=[
                RetrievedPage(
                    page=page,
                    score=0.9,
                    role="focus_concept",
                )
            ],
            embedding_units_by_page_id={},
        )

        self.assertEqual(len(snippets), 1)
        self.assertEqual(snippets[0].source_document_id, "doc_a")
        self.assertEqual(snippets[0].source_block_ids, ["B0001"])
        self.assertEqual(
            [(ref.source_document_id, ref.source_block_id) for ref in snippets[0].source_refs],
            [("doc_a", "B0001"), ("doc_b", "B0008")],
        )
        self.assertEqual(snippets[0].text, "여러 원문을 함께 참조하는 근거입니다.")

    def test_supports_short_refs_with_single_frontmatter_source(self) -> None:
        page = WikiPage(
            id="concept:citable",
            page_type="concept",
            title="Citable Concept",
            slug="citable-concept",
            summary="Citable summary",
            markdown=(
                "---\nsources: doc_citable\n---\n\n"
                "앞단 필터와 선택기가 함께 인식해야 하는 근거입니다. [B0001]"
            ),
        )
        selector = EvidenceSelector(
            embedding_search=QueryContainsSearch(),
            text_search=EmptySearch(),
        )

        snippets = selector.select(
            question="함께 인식해야 하는 근거",
            related_pages=[RetrievedPage(page=page, score=0.9, role="focus_concept")],
            embedding_units_by_page_id={},
        )

        self.assertEqual(snippets[0].source_document_id, "doc_citable")
        self.assertEqual(snippets[0].source_block_ids, ["B0001"])

    def test_rejects_invalid_and_missing_source_refs(self) -> None:
        invalid_page = WikiPage(
            id="concept:invalid",
            page_type="concept",
            title="Invalid Concept",
            slug="invalid-concept",
            summary="Invalid summary",
            markdown="---\nsources: doc_invalid\n---\n\n본문입니다. [not-a-block]",
        )
        missing_page = WikiPage(
            id="concept:missing",
            page_type="concept",
            title="Missing Concept",
            slug="missing-concept",
            summary="Missing summary",
            markdown="---\nsources: doc_missing\n---\n\n인용 없는 본문입니다.",
        )
        selector = EvidenceSelector(
            embedding_search=EmptySearch(),
            text_search=EmptySearch(),
        )

        snippets = selector.select(
            question="본문",
            related_pages=[
                RetrievedPage(page=invalid_page, score=0.9, role="focus_concept"),
                RetrievedPage(page=missing_page, score=0.8, role="focus_concept"),
            ],
            embedding_units_by_page_id={},
        )

        self.assertEqual(snippets, [])

    def test_rejects_malformed_stored_units_but_keeps_valid_units(self) -> None:
        malformed_page = WikiPage(
            id="concept:malformed-unit",
            page_type="concept",
            title="Malformed Unit",
            slug="malformed-unit",
            summary="Malformed summary",
        )
        valid_page = WikiPage(
            id="concept:valid-unit",
            page_type="concept",
            title="Valid Unit",
            slug="valid-unit",
            summary="Valid summary",
        )
        selector = EvidenceSelector(embedding_search=EmptySearch(), text_search=EmptySearch())

        snippets = selector.select(
            question="근거",
            related_pages=[
                RetrievedPage(page=malformed_page, score=1.0, role="focus_concept"),
                RetrievedPage(page=valid_page, score=0.8, role="focus_concept"),
            ],
            embedding_units_by_page_id={
                malformed_page.id: [
                    WikiEmbeddingUnit(
                        "malformed",
                        malformed_page.id,
                        "doc-malformed",
                        "evidence",
                        ["not-a-block"],
                        "잘못된 저장 unit",
                    )
                ],
                valid_page.id: [
                    WikiEmbeddingUnit(
                        "valid",
                        valid_page.id,
                        "doc-valid",
                        "evidence",
                        ["B0001"],
                        "유효한 저장 unit",
                    )
                ],
            },
        )

        self.assertEqual([snippet.source_document_id for snippet in snippets], ["doc-valid"])

    def test_returns_only_global_top_k_evidence(self) -> None:
        first_page = WikiPage(
            id="concept:first",
            page_type="concept",
            title="첫 번째 개념",
            slug="first",
            summary="첫 번째 개념 요약",
        )
        second_page = WikiPage(
            id="concept:second",
            page_type="concept",
            title="두 번째 개념",
            slug="second",
            summary="두 번째 개념 요약",
        )
        selector = EvidenceSelector(
            embedding_search=EmptySearch(),
            text_search=EmptySearch(),
            max_evidence_snippets=2,
        )

        snippets = selector.select(
            question="복합 질문",
            related_pages=[
                RetrievedPage(page=second_page, score=0.9, role="focus_concept"),
                RetrievedPage(page=first_page, score=0.5, role="focus_concept"),
            ],
            embedding_units_by_page_id={
                first_page.id: [
                    WikiEmbeddingUnit("first-1", first_page.id, "doc-first", "evidence", ["B0001"], "첫 근거"),
                    WikiEmbeddingUnit("first-2", first_page.id, "doc-first", "evidence", ["B0002"], "둘째 근거"),
                ],
                second_page.id: [
                    WikiEmbeddingUnit("second-1", second_page.id, "doc-second", "evidence", ["B0001"], "다른 페이지 근거"),
                ],
            },
        )

        self.assertEqual(len(snippets), 2)
        self.assertEqual(snippets[0].source_document_id, "doc-second")

    def test_global_ranking_does_not_force_page_coverage(self) -> None:
        first_page = WikiPage(
            id="concept:first-coverage",
            page_type="concept",
            title="첫 번째 개념",
            slug="first-coverage",
            summary="첫 번째 개념 요약",
        )
        second_page = WikiPage(
            id="concept:second-coverage",
            page_type="concept",
            title="두 번째 개념",
            slug="second-coverage",
            summary="두 번째 개념 요약",
        )
        third_page = WikiPage(
            id="concept:third-coverage",
            page_type="concept",
            title="세 번째 개념",
            slug="third-coverage",
            summary="세 번째 개념 요약",
        )
        selector = EvidenceSelector(
            embedding_search=EmptySearch(),
            text_search=EmptySearch(),
            max_evidence_snippets=2,
        )

        snippets = selector.select(
            question="관련 없는 질문",
            related_pages=[
                RetrievedPage(page=first_page, score=0.9, role="focus_concept"),
                RetrievedPage(page=second_page, score=0.95, role="focus_concept"),
                RetrievedPage(page=third_page, score=0.85, role="focus_concept"),
            ],
            embedding_units_by_page_id={
                first_page.id: [
                    WikiEmbeddingUnit(
                        "first-covered",
                        first_page.id,
                        "doc-first",
                        "evidence",
                        ["B0001"],
                        "첫 번째 페이지 coverage",
                        1.0,
                    ),
                    WikiEmbeddingUnit(
                        "first-extra",
                        first_page.id,
                        "doc-first",
                        "evidence",
                        ["B0002"],
                        "첫 번째 페이지 extra",
                        0.9,
                    ),
                ],
                second_page.id: [
                    WikiEmbeddingUnit(
                        "second-covered",
                        second_page.id,
                        "doc-second",
                        "evidence",
                        ["B0001"],
                        "두 번째 페이지 coverage",
                        0.1,
                    )
                ],
                third_page.id: [
                    WikiEmbeddingUnit(
                        "third-covered",
                        third_page.id,
                        "doc-third",
                        "evidence",
                        ["B0001"],
                        "세 번째 페이지 coverage",
                        0.1,
                    )
                ],
            },
        )

        self.assertEqual(
            [snippet.source_document_id for snippet in snippets],
            ["doc-first", "doc-first"],
        )


if __name__ == "__main__":
    unittest.main()
