import unittest
from dataclasses import replace

from app.modules.query.application.answer_query import AnswerQueryUseCase, _fallback_language
from app.modules.query.application.query_page_scorer import QueryPageScorer
from app.modules.query.application.traverse_wiki_graph import TraverseWikiGraphUseCase
from app.modules.query.domain.entities import (
    ConversationContext,
    GeneratedAnswer,
    QueryContext,
    QueryEvaluation,
    QueryRewrite,
    SemanticQueryEmbedding,
    WebSearchResult,
    WikiPage,
    WikiPageLink,
)
from app.modules.query.domain.exceptions import InvalidQuestionError
from app.modules.query.infrastructure.in_memory_wiki_repository import InMemoryWikiRepository
from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
from app.modules.query.infrastructure.rule_based_query_rewriter import RuleBasedQueryRewriter


class ScoreSearch:
    def __init__(self, scores_by_title: dict[str, float]) -> None:
        self._scores_by_title = scores_by_title

    def score(self, query: str, documents: list[str]) -> list[float]:
        return [self._scores_by_title.get(document.splitlines()[0], 0.0) for document in documents]


class RecordingScoreSearch(ScoreSearch):
    def __init__(self, scores_by_title: dict[str, float]) -> None:
        super().__init__(scores_by_title)
        self.queries: list[str] = []

    def score(self, query: str, documents: list[str]) -> list[float]:
        self.queries.append(query)
        return super().score(query, documents)


class SemanticScoreSearch(ScoreSearch):
    def embed_query(self, query: str) -> SemanticQueryEmbedding:
        return SemanticQueryEmbedding(model_name="test-model", vector=[1.0, 0.0])


class RecordingSemanticScoreSearch(RecordingScoreSearch):
    def __init__(self, scores_by_title: dict[str, float]) -> None:
        super().__init__(scores_by_title)
        self.embedding_queries: list[str] = []

    def embed_query(self, query: str) -> SemanticQueryEmbedding:
        self.embedding_queries.append(query)
        return SemanticQueryEmbedding(model_name="test-model", vector=[1.0, 0.0])


class EmptyTextSearch:
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


class SourceStructureIntentSearch:
    def score(self, query: str, documents: list[str]) -> list[float]:
        intents = [
            ("과학개념 추출", "Categories", "과학 개념"),
            ("응결이 뭐야", "Core Concepts", "응결"),
            ("강수 과정 섹션", "Section Candidates", "강수 과정"),
            ("지반이 언급", "Mentions", "지반"),
        ]
        for query_marker, section_name, content_marker in intents:
            if query_marker in query:
                return [
                    1.0 if document.startswith(f"{section_name}\n") and content_marker in document else 0.0
                    for document in documents
                ]
        return [0.0 for _ in documents]


class FakeMarkdownReader:
    def __init__(self, markdown_by_uri: dict[str, str]) -> None:
        self._markdown_by_uri = markdown_by_uri

    def read_markdown(self, markdown_uri: str) -> str:
        return self._markdown_by_uri[markdown_uri]


class RecordingEventPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object] | None]] = []

    def publish(self, stage: str, message: str, data: dict[str, object] | None = None) -> None:
        self.events.append((stage, message, data))


class RecordingAnswerGenerator:
    def __init__(self, content: str = "테스트 답변입니다. [1]") -> None:
        self.content = content
        self.last_context: QueryContext | None = None

    def generate_answer(self, context: QueryContext) -> GeneratedAnswer:
        self.last_context = context
        return GeneratedAnswer(content=self.content)


class SequencedAnswerGenerator:
    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.contexts: list[QueryContext] = []

    def generate_answer(self, context: QueryContext) -> GeneratedAnswer:
        self.contexts.append(context)
        index = min(len(self.contexts) - 1, len(self.contents) - 1)
        return GeneratedAnswer(content=self.contents[index])


class FixedQueryRewriter:
    def __init__(self, rewritten_query: str) -> None:
        self.rewritten_query = rewritten_query

    def rewrite(self, question: str) -> QueryRewrite:
        return QueryRewrite(original_question=question, retrieval_query=self.rewritten_query, keywords=self.rewritten_query.split())


class FakeWebSearch:
    def __init__(self, results: list[WebSearchResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, query: str) -> list[WebSearchResult]:
        self.queries.append(query)
        return self.results


class FailingWebSearch:
    def search(self, query: str) -> list[WebSearchResult]:
        raise RuntimeError("provider detail must not reach the query result")


class FakeQueryEvaluator:
    def __init__(self, evaluation: QueryEvaluation | list[QueryEvaluation]) -> None:
        self.evaluations = evaluation if isinstance(evaluation, list) else [evaluation]
        self.calls: list[tuple[str, QueryContext, GeneratedAnswer, str, bool]] = []

    def evaluate(
        self,
        question: str,
        context: QueryContext,
        answer: GeneratedAnswer,
        stop_reason: str,
        web_search_available: bool = False,
    ) -> QueryEvaluation:
        index = min(len(self.calls), len(self.evaluations) - 1)
        self.calls.append((question, context, answer, stop_reason, web_search_available))
        return self.evaluations[index]


class RecordingCandidateRepository(InMemoryWikiRepository):
    def __init__(
        self,
        pages: list[WikiPage],
        links: list[WikiPageLink],
    ) -> None:
        super().__init__(pages, links)
        self.candidate_calls: list[tuple[str, str, int, int]] = []
        self.semantic_queries: list[SemanticQueryEmbedding | None] = []
        self.link_calls: list[
            tuple[str, list[str], int, list[str] | None]
        ] = []
        self.page_id_calls: list[tuple[str, list[str]]] = []

    def list_candidate_pages(
        self,
        workspace_id: str,
        query: str,
        source_limit: int,
        concept_limit: int,
        semantic_query: SemanticQueryEmbedding | None = None,
    ) -> list[WikiPage]:
        self.candidate_calls.append(
            (workspace_id, query, source_limit, concept_limit)
        )
        self.semantic_queries.append(semantic_query)
        return super().list_candidate_pages(
            workspace_id,
            query,
            source_limit,
            concept_limit,
            semantic_query,
        )

    def list_links_for_page_ids(
        self,
        workspace_id: str,
        page_ids: list[str],
        limit: int,
        excluded_page_ids: list[str] | None = None,
    ) -> list[WikiPageLink]:
        self.link_calls.append(
            (workspace_id, page_ids, limit, excluded_page_ids)
        )
        return super().list_links_for_page_ids(
            workspace_id,
            page_ids,
            limit,
            excluded_page_ids,
        )

    def list_pages_by_ids(
        self,
        workspace_id: str,
        page_ids: list[str],
    ) -> list[WikiPage]:
        self.page_id_calls.append((workspace_id, page_ids))
        return super().list_pages_by_ids(workspace_id, page_ids)


def source_page(page_id: str, title: str) -> WikiPage:
    return WikiPage(
        id=page_id,
        page_type="source",
        title=title,
        slug=title.lower().replace(" ", "-"),
        summary=f"{title} 요약",
        markdown_uri=f"s3://test/{page_id}.md",
    )


def concept_page(page_id: str, title: str) -> WikiPage:
    return WikiPage(
        id=page_id,
        page_type="concept",
        title=title,
        slug=title.lower().replace(" ", "-"),
        summary=f"{title} 정의",
        markdown_uri=f"s3://test/{page_id}.md",
    )


class AnswerQueryUseCaseTest(unittest.TestCase):
    def test_retrieve_evidence_skips_answer_generation(self) -> None:
        answer_generator = RecordingAnswerGenerator()
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(
                [source_page("source:retrieval", "Retrieval Source")],
                [],
            ),
            embedding_search=ScoreSearch({"Retrieval Source": 1.0}),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:retrieval.md": (
                        "---\ndocument_id: doc_retrieval\n---\n\n"
                        "검색된 원문 근거입니다. [B0001]"
                    )
                }
            ),
        )

        result = use_case.retrieve_evidence(
            "원문 근거는?",
            workspace_id="ws_test",
            user_id="user_test",
        )

        self.assertIsNone(answer_generator.last_context)
        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["B0001"])
        self.assertEqual(result.retrieval_summary.used_source_count, 1)

    def test_bounds_repository_candidates_before_scoring(self) -> None:
        pages = [
            source_page("source:bounded", "Bounded Source"),
            concept_page("concept:bounded", "Bounded Concept"),
        ]
        repository = RecordingCandidateRepository(pages, [])
        use_case = AnswerQueryUseCase(
            wiki_repository=repository,
            embedding_search=ScoreSearch(
                {
                    "Bounded Source": 0.9,
                    "Bounded Concept": 0.8,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:bounded.md": "---\ndocument_id: doc_bounded\n---\n\nBounded source. [B0001]",
                    "s3://test/concept:bounded.md": "Bounded concept. [B0002]",
                }
            ),
            source_candidate_limit=3,
            concept_candidate_limit=2,
            candidate_pool_multiplier=4,
            graph_link_limit=7,
        )

        use_case.execute("bounded 질문", workspace_id="ws_test")

        self.assertEqual(
            repository.candidate_calls,
            [("ws_test", "bounded 질문", 12, 8)],
        )
        self.assertEqual(repository.link_calls[0][0], "ws_test")
        self.assertEqual(
            repository.link_calls[0][1],
            ["concept:bounded", "source:bounded"],
        )
        self.assertEqual(repository.link_calls[0][2], 7)
        self.assertEqual(repository.link_calls[0][3], [])
        self.assertEqual(repository.page_id_calls, [("ws_test", [])])

    def test_passes_query_embedding_for_global_semantic_candidates(self) -> None:
        repository = RecordingCandidateRepository(
            [source_page("source:semantic", "Semantic Source")],
            [],
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=repository,
            embedding_search=SemanticScoreSearch({"Semantic Source": 0.9}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:semantic.md": (
                        "---\ndocument_id: doc_semantic\n---\n\nSemantic source. [B0001]"
                    ),
                }
            ),
        )

        use_case.execute("의미 검색", workspace_id="ws_test")

        self.assertEqual(
            repository.semantic_queries,
            [SemanticQueryEmbedding(model_name="test-model", vector=[1.0, 0.0])],
        )

    def test_uses_dense_score_for_page_ranking(self) -> None:
        pages = [
            source_page("source:semantic", "Semantic Candidate"),
            source_page("source:keyword", "Keyword Candidate"),
        ]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch(
                {
                    "Semantic Candidate": 0.9,
                    "Keyword Candidate": 0.4,
                }
            ),
            text_search=QueryContainsSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:semantic.md": (
                        "---\ndocument_id: doc_semantic\n---\n\n다른 내용입니다. [B0001]"
                    ),
                    "s3://test/source:keyword.md": (
                        "---\ndocument_id: doc_keyword\n---\n\n정확한검색어가 있습니다. [B0002]"
                    ),
                }
            ),
            source_candidate_limit=1,
        )

        result = use_case.execute("정확한검색어", workspace_id="ws_test")

        self.assertEqual(result.related_pages[0].page.id, "source:semantic")

    def test_hybrid_page_ranking_preserves_target_evidence(self) -> None:
        pages = [
            source_page("source:target", "Hybrid Target"),
            source_page("source:distractor", "Hybrid Distractor"),
        ]
        query = "정확한검색어가 뭐야"
        markdown_by_uri = {
            "s3://test/source:target.md": (
                "---\ndocument_id: doc_target\n---\n\n"
                "정확한검색어 설명하는 target evidence입니다. [B0001]"
            ),
            "s3://test/source:distractor.md": (
                "---\ndocument_id: doc_distractor\n---\n\n"
                "관련 없는 distractor evidence입니다. [B0002]"
            ),
        }
        scoring_pages = [
            replace(page, markdown=markdown_by_uri[page.markdown_uri])
            for page in pages
        ]
        text_search = Bm25Searcher()
        query_rewrite = RuleBasedQueryRewriter().rewrite(query)
        representations = [
            "\n".join([page.title, page.summary, page.markdown or ""]).strip()
            for page in scoring_pages
        ]
        self.assertEqual(query_rewrite.retrieval_query, "정확한검색어")
        self.assertEqual(
            text_search.score(query_rewrite.retrieval_query, representations),
            [1.0, 0.0],
        )
        page_scorer = QueryPageScorer(
            embedding_search=ScoreSearch(
                {
                    "Hybrid Target": 0.80,
                    "Hybrid Distractor": 0.95,
                }
            ),
            text_search=text_search,
        )
        dense_only_scores = page_scorer.score_pages(
            query_rewrite,
            scoring_pages,
            embedding_weight=1.0,
        )
        hybrid_scores = page_scorer.score_pages(
            query_rewrite,
            scoring_pages,
            embedding_weight=0.8,
        )
        self.assertGreater(
            dense_only_scores["source:distractor"],
            dense_only_scores["source:target"],
        )
        self.assertAlmostEqual(hybrid_scores["source:target"], 0.84)
        self.assertAlmostEqual(hybrid_scores["source:distractor"], 0.76)
        self.assertGreater(
            hybrid_scores["source:target"],
            hybrid_scores["source:distractor"],
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch(
                {
                    "Hybrid Target": 0.80,
                    "Hybrid Distractor": 0.95,
                }
            ),
            text_search=text_search,
            answer_generator=RecordingAnswerGenerator(),
            query_rewriter=RuleBasedQueryRewriter(),
            markdown_reader=FakeMarkdownReader(
                markdown_by_uri
            ),
            source_candidate_limit=1,
        )

        result = use_case.execute(query, workspace_id="ws_test")

        self.assertEqual(result.related_pages[0].page.id, "source:target")
        self.assertAlmostEqual(result.related_pages[0].score, 0.84)
        self.assertEqual(result.evidence_snippets[0].source_document_id, "doc_target")
        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["B0001"])
        self.assertIn("target evidence", result.evidence_snippets[0].text)

    def test_loads_neighbor_page_outside_initial_candidate_pool(self) -> None:
        pages = [
            source_page("source:seed", "Seed Source"),
            concept_page("concept:neighbor", "Neighbor Concept"),
        ]
        links = [
            WikiPageLink(
                from_page_id="source:seed",
                to_page_id="concept:neighbor",
                link_type="source_mentions_concept",
                confidence=0.95,
            )
        ]
        repository = RecordingCandidateRepository(pages, links)
        use_case = AnswerQueryUseCase(
            wiki_repository=repository,
            embedding_search=ScoreSearch(
                {
                    "Seed Source": 0.9,
                    "Neighbor Concept": 0.9,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:seed.md": (
                        "---\ndocument_id: doc_seed\n---\n\nSeed. [B0001]"
                    ),
                    "s3://test/concept:neighbor.md": "Neighbor. [B0002]",
                }
            ),
            source_candidate_limit=1,
            concept_candidate_limit=0,
            candidate_pool_multiplier=1,
        )

        result = use_case.execute("seed 질문", workspace_id="ws_test")

        self.assertEqual(
            repository.page_id_calls,
            [("ws_test", ["concept:neighbor"])],
        )
        self.assertIn(
            "concept:neighbor",
            {item.page.id for item in result.related_pages},
        )

    def test_graph_link_limit_counts_only_traversable_relations(self) -> None:
        pages = [
            source_page("source:seed", "Seed Source"),
            concept_page("concept:excluded", "Excluded Concept"),
            concept_page("concept:allowed", "Allowed Concept"),
        ]
        repository = InMemoryWikiRepository(
            pages,
            [
                WikiPageLink(
                    "source:seed",
                    "concept:excluded",
                    "related_evidence",
                    confidence=0.99,
                ),
                WikiPageLink(
                    "source:seed",
                    "concept:allowed",
                    "part_of",
                    confidence=0.98,
                ),
            ],
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=repository,
            embedding_search=ScoreSearch(
                {
                    "Seed Source": 0.98,
                    "Excluded Concept": 0.99,
                    "Allowed Concept": 1.0,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:seed.md": "---\ndocument_id: doc_seed\n---\n\nSeed. [B0001]",
                    "s3://test/concept:excluded.md": "Excluded. [B0002]",
                    "s3://test/concept:allowed.md": (
                        "---\nsources: doc_allowed\n---\n\nAllowed evidence. [B0003]"
                    ),
                }
            ),
            source_candidate_limit=1,
            concept_candidate_limit=0,
            candidate_pool_multiplier=1,
            graph_link_limit=1,
        )

        result = use_case.execute("allowed evidence", workspace_id="ws_test")

        self.assertIn(
            "concept:allowed",
            {item.page.id for item in result.related_pages},
        )
        self.assertEqual(result.evidence_snippets[0].source_document_id, "doc_allowed")
        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["B0003"])
        self.assertIn("[1]", result.answer.content)

    def test_expands_bounded_graph_to_configured_depth(self) -> None:
        pages = [
            source_page("source:seed", "Seed Source"),
            concept_page("concept:one", "Concept One"),
            concept_page("concept:two", "Concept Two"),
            concept_page("concept:three", "Concept Three"),
        ]
        links = [
            WikiPageLink(
                from_page_id="source:seed",
                to_page_id="concept:one",
                link_type="source_mentions_concept",
                confidence=0.95,
            ),
            WikiPageLink(
                from_page_id="concept:one",
                to_page_id="concept:two",
                link_type="concept_related_to",
                confidence=0.95,
            ),
            WikiPageLink(
                from_page_id="concept:two",
                to_page_id="concept:three",
                link_type="concept_related_to",
                confidence=0.95,
            ),
        ]
        repository = RecordingCandidateRepository(pages, links)
        markdown = {
            page.markdown_uri: (
                "---\ndocument_id: doc_seed\n---\n\nEvidence. [B0001]"
                if page.is_source
                else f"{page.title}. [B0001]"
            )
            for page in pages
            if page.markdown_uri
        }
        use_case = AnswerQueryUseCase(
            wiki_repository=repository,
            embedding_search=ScoreSearch(
                {page.title: 0.9 for page in pages}
            ),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(markdown),
            source_candidate_limit=1,
            concept_candidate_limit=0,
            candidate_pool_multiplier=1,
            graph_expansion_depth=3,
        )

        result = use_case.execute("seed 질문", workspace_id="ws_test")

        self.assertIn(
            "concept:three",
            {item.page.id for item in result.related_pages},
        )
        self.assertEqual(
            [call[1] for call in repository.link_calls],
            [["source:seed"], ["concept:one"], ["concept:two"]],
        )

    def test_starts_from_top_source_and_cites_evidence_context(self) -> None:
        pages = [
            source_page("source:attention", "Lecture Attention"),
            source_page("source:unrelated", "Unrelated Source"),
            concept_page("concept:self-attention", "Self Attention"),
        ]
        links = [
            WikiPageLink(
                from_page_id="source:attention",
                to_page_id="concept:self-attention",
                link_type="source_mentions_concept",
                confidence=0.95,
            )
        ]
        answer_generator = RecordingAnswerGenerator()
        event_publisher = RecordingEventPublisher()
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, links),
            embedding_search=ScoreSearch(
                {
                    "Lecture Attention": 0.90,
                    "Unrelated Source": 0.10,
                    "Self Attention": 0.95,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:attention.md": "---\ndocument_id: doc_attention\n---\n\nSelf-attention computes relationships between tokens in one sequence. [B0001]",
                    "s3://test/source:unrelated.md": "---\ndocument_id: doc_unrelated\n---\n\nUnrelated source body. [B0009]",
                    "s3://test/concept:self-attention.md": "---\ntype: concept\nsources: doc_attention\n---\n\n입력 토큰들이 서로를 참조해 중요도를 계산하는 Transformer 메커니즘. [B0002]",
                }
            ),
            source_candidate_limit=1,
            event_publisher=event_publisher,
            traverse_wiki_graph=TraverseWikiGraphUseCase(),
        )

        result = use_case.execute("토큰끼리 서로 보는 구조가 뭐야?", workspace_id="ws_test")

        related_ids = {item.page.id for item in result.related_pages}
        self.assertIn("source:attention", related_ids)
        self.assertNotIn("source:unrelated", related_ids)
        self.assertIn("concept:self-attention", related_ids)
        self.assertTrue(
            any("source:attention" in path.nodes and "concept:self-attention" in path.nodes for path in result.traversal_paths)
        )
        self.assertIsNotNone(answer_generator.last_context)
        self.assertIn("# User Question", answer_generator.last_context.answer_context)
        self.assertIn("# Evidence Snippets By Relevance", answer_generator.last_context.answer_context)
        self.assertIn("Answer in Korean.", answer_generator.last_context.answer_context)
        self.assertIn("Do not create examples, analogies, or fictional cases", answer_generator.last_context.answer_context)
        self.assertIn("Self-attention computes relationships between tokens", answer_generator.last_context.answer_context)
        self.assertIn("입력 토큰들이 서로를 참조", answer_generator.last_context.answer_context)
        self.assertNotIn("[B0001]", answer_generator.last_context.answer_context)
        self.assertIn("citation markers like [1]", answer_generator.last_context.answer_context)
        self.assertNotIn("/api/wiki/pages/concept:self-attention", answer_generator.last_context.answer_context)
        self.assertIn("Lecture Attention -> Self Attention", answer_generator.last_context.answer_context)
        self.assertGreaterEqual(len(answer_generator.last_context.evidence_snippets), 2)
        self.assertEqual(len(result.evidence_snippets), 1)
        self.assertEqual(result.evidence_snippets[0].rank, 1)
        self.assertEqual(result.evidence_snippets[0].source_document_id, "doc_attention")
        self.assertTrue(result.evidence_snippets[0].source_block_ids)
        self.assertIn("토큰", result.evidence_snippets[0].text)
        self.assertNotIn("[B", result.evidence_snippets[0].text)
        self.assertNotIn("Unrelated", result.evidence_snippets[0].text)
        self.assertIn("[1]", result.answer.content)
        self.assertEqual(result.retrieval_summary.max_depth, 0)
        self.assertNotIn("# User Question", result.answer.content)
        event_stages = [event[0] for event in event_publisher.events]
        self.assertEqual(
            event_stages,
            [
                "query_started",
                "wiki_loaded",
                "retrieval_markdown_loaded",
                "retrieval_scored",
                "seeds_selected",
                "graph_traversed",
                "markdown_loaded",
                "context_built",
                "answer_generated",
            ],
        )

    def test_renumbers_answer_citations_to_used_evidence_order(self) -> None:
        pages = [
            source_page(
                "source:attention",
                "Lecture Attention",
            )
        ]
        answer_generator = RecordingAnswerGenerator(
            content="첫 번째 근거를 사용합니다. [1] 세 번째 근거도 사용합니다. [3]"
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"Lecture Attention": 0.95}),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:attention.md": (
                        "---\ndocument_id: doc_attention\n---\n\n"
                        "토큰 관계를 설명하는 첫 번째 근거입니다. [B0001]\n\n"
                        "토큰 관계를 보조 설명하는 두 번째 근거입니다. [B0002]\n\n"
                        "토큰 관계를 다시 설명하는 세 번째 근거입니다. [B0003]"
                    )
                }
            ),
        )

        result = use_case.execute("토큰 관계 설명", workspace_id="ws_test")

        self.assertIn("[1]", result.answer.content)
        self.assertIn("[2]", result.answer.content)
        self.assertNotIn("[3]", result.answer.content)
        self.assertEqual([snippet.rank for snippet in result.evidence_snippets], [1, 2])
        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["B0001"])
        self.assertEqual(result.evidence_snippets[1].source_block_ids, ["B0003"])

    def test_ignores_legacy_source_related_to_edges(self) -> None:
        pages = [
            source_page("source:a", "RAG Overview"),
            source_page("source:b", "Wiki Graph Notes"),
            concept_page("concept:rag", "RAG"),
        ]
        links = [
            WikiPageLink(
                from_page_id="source:a",
                to_page_id="source:b",
                link_type="source_related_to",
                confidence=0.92,
            ),
        ]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, links),
            embedding_search=ScoreSearch(
                {
                    "RAG Overview": 0.95,
                    "Wiki Graph Notes": 0.94,
                    "RAG": 0.20,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            source_candidate_limit=1,
            traverse_wiki_graph=TraverseWikiGraphUseCase(),
        )

        result = use_case.execute("RAG랑 위키 그래프가 어떻게 이어져?", workspace_id="ws_test")

        related_ids = {item.page.id for item in result.related_pages}
        edge_types = {edge.link_type for edge in result.graph_context.edges}
        self.assertNotIn("source:b", related_ids)
        self.assertNotIn("source_related_to", edge_types)

    def test_excludes_nodes_below_five_percent_from_best_observed_score(self) -> None:
        pages = [
            source_page("source:seed", "Seed Source"),
            concept_page("concept:near", "Near Concept"),
            concept_page("concept:far", "Far Concept"),
        ]
        links = [
            WikiPageLink("source:seed", "concept:near", "source_mentions_concept", confidence=0.99),
            WikiPageLink("source:seed", "concept:far", "source_mentions_concept", confidence=0.99),
        ]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, links),
            embedding_search=ScoreSearch(
                {
                    "Seed Source": 1.00,
                    "Near Concept": 0.96,
                    "Far Concept": 0.94,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            source_candidate_limit=1,
        )

        result = use_case.execute("상대 점수 제한 확인", workspace_id="ws_test")

        related_ids = {item.page.id for item in result.related_pages}
        self.assertIn("concept:near", related_ids)
        self.assertNotIn("concept:far", related_ids)

    def test_returns_only_selected_answer_paths(self) -> None:
        pages = [
            source_page("source:seed", "Seed Source"),
            concept_page("concept:one", "Concept One"),
            concept_page("concept:two", "Concept Two"),
        ]
        links = [
            WikiPageLink("source:seed", "concept:one", "source_mentions_concept", confidence=0.99),
            WikiPageLink("source:seed", "concept:two", "source_mentions_concept", confidence=0.99),
        ]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, links),
            embedding_search=ScoreSearch(
                {
                    "Seed Source": 1.00,
                    "Concept One": 0.99,
                    "Concept Two": 0.98,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            source_candidate_limit=1,
            returned_path_limit=1,
        )

        result = use_case.execute("path 반환 제한 확인", workspace_id="ws_test")

        self.assertEqual(len(result.traversal_paths), 1)
        self.assertEqual(result.traversal_paths[0].role, "primary_answer_path")

    def test_does_not_expand_graph_when_top_source_score_is_zero(self) -> None:
        pages = [
            source_page("source:seed", "Seed Source"),
            concept_page("concept:connected", "Connected Concept"),
        ]
        links = [
            WikiPageLink("source:seed", "concept:connected", "source_mentions_concept", confidence=0.99),
        ]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, links),
            embedding_search=ScoreSearch(
                {
                    "Seed Source": 0.0,
                    "Connected Concept": 0.0,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            source_candidate_limit=1,
        )

        result = use_case.execute("관련 없는 질문", workspace_id="ws_test")

        related_ids = {item.page.id for item in result.related_pages}
        self.assertEqual(related_ids, {"source:seed"})
        self.assertEqual(result.traversal_paths, [])
        self.assertEqual(result.retrieval_summary.stop_reason, "no_relevant_seed")
        self.assertIn("제공된 근거에서 질문에 직접 답할 내용을 찾지 못했습니다.", result.answer.content)
        self.assertNotIn("Transformer", result.answer.content)

    def test_uses_original_question_for_dense_and_rewrite_for_text_retrieval(self) -> None:
        pages = [
            source_page("source:attention", "Lecture Attention"),
            concept_page("concept:self-attention", "Self Attention"),
        ]
        repository = RecordingCandidateRepository(pages, [])
        embedding_search = RecordingSemanticScoreSearch(
            {
                "Lecture Attention": 0.90,
                "Self Attention": 0.95,
            }
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=repository,
            embedding_search=embedding_search,
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            query_rewriter=FixedQueryRewriter("self attention token"),
        )

        use_case.execute("토큰끼리 서로 보는 구조가 뭐야?", workspace_id="ws_test")

        self.assertEqual(
            embedding_search.embedding_queries,
            ["토큰끼리 서로 보는 구조가 뭐야?"],
        )
        self.assertTrue(embedding_search.queries)
        self.assertTrue(
            all(query == "토큰끼리 서로 보는 구조가 뭐야?" for query in embedding_search.queries)
        )
        self.assertEqual(repository.candidate_calls[0][1], "self attention token")

    def test_backfills_lower_ranked_concept_when_top_page_has_no_evidence(self) -> None:
        uncitable = concept_page("concept:uncitable", "Uncitable Concept")
        citable = concept_page("concept:citable", "Citable Concept")
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository([uncitable, citable], []),
            embedding_search=ScoreSearch(
                {
                    "Uncitable Concept": 1.0,
                    "Citable Concept": 0.3,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    citable.markdown_uri: (
                        "---\nsources: doc_citable\n---\n\n"
                        "질문을 뒷받침하는 근거입니다. [B0001]"
                    ),
                }
            ),
        )

        result = use_case.execute("근거가 필요한 질문", workspace_id="ws_test")

        self.assertEqual(result.evidence_snippets[0].source_document_id, "doc_citable")
        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["B0001"])

    def test_backfills_citable_concepts_near_the_top_score(self) -> None:
        first = concept_page("concept:first", "First Concept")
        second = concept_page("concept:second", "Second Concept")
        answer_generator = RecordingAnswerGenerator()
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository([first, second], []),
            embedding_search=ScoreSearch(
                {
                    "First Concept": 1.0,
                    "Second Concept": 0.88,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    first.markdown_uri: (
                        "---\nsources: doc_first\n---\n\n"
                        "첫 번째 근거입니다. [doc_first:B0001]"
                    ),
                    second.markdown_uri: (
                        "---\nsources: doc_second\n---\n\n"
                        "두 번째 근거입니다. [doc_second:B0001]"
                    ),
                }
            ),
        )

        use_case.execute("두 근거를 함께 설명해줘", workspace_id="ws_test")

        self.assertIsNotNone(answer_generator.last_context)
        self.assertEqual(
            {
                snippet.source_document_id
                for snippet in answer_generator.last_context.evidence_snippets
            },
            {"doc_first", "doc_second"},
        )

    def test_uses_rewritten_query_for_text_only_page_scoring(self) -> None:
        pages = [source_page("source:attention", "Self Attention")]
        text_search = RecordingScoreSearch({"Self Attention": 0.95})
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=text_search,
            text_search=text_search,
            answer_generator=RecordingAnswerGenerator(),
            query_rewriter=FixedQueryRewriter("self attention token"),
        )

        use_case.execute("토큰끼리 서로 보는 구조가 뭐야?", workspace_id="ws_test")

        self.assertEqual(set(text_search.queries), {"self attention token"})

    def test_uses_conversation_context_to_resolve_follow_up_question_for_retrieval(self) -> None:
        pages = [
            source_page("source:wiki", "LLM Wiki Source"),
            concept_page("concept:persistent-wiki", "Persistent Wiki"),
        ]
        embedding_search = RecordingScoreSearch(
            {
                "LLM Wiki Source": 0.95,
                "Persistent Wiki": 0.94,
            }
        )
        answer_generator = RecordingAnswerGenerator()
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(
                pages,
                [
                    WikiPageLink(
                        from_page_id="source:wiki",
                        to_page_id="concept:persistent-wiki",
                        link_type="source_mentions_concept",
                        confidence=0.95,
                    )
                ],
            ),
            embedding_search=embedding_search,
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:wiki.md": (
                        "---\ndocument_id: doc_wiki\n---\n\n"
                        "Persistent Wiki는 기존 RAG 시스템과 달리 지식을 지속적으로 축적합니다. [B0001]"
                    ),
                    "s3://test/concept:persistent-wiki.md": (
                        "---\ntype: concept\nsources: doc_wiki\n---\n\n"
                        "Persistent Wiki는 누적되는 위키 구조입니다. [B0002]"
                    ),
                }
            ),
        )

        result = use_case.execute(
            "그거랑 RAG 차이는?",
            workspace_id="ws_test",
            conversation_context=ConversationContext(
                recent_conversation_summary="사용자는 Persistent Wiki와 RAG의 차이를 이어서 묻고 있다.",
                reference_context={
                    "active_topic": {"canonical": "Persistent Wiki", "aliases": ["지속적 위키"]},
                    "recent_concepts": ["Persistent Wiki", "RAG"],
                    "referents": {"그거": {"canonical": "Persistent Wiki", "aliases": ["지속적 위키"]}},
                },
            ),
        )

        self.assertTrue(embedding_search.queries)
        self.assertTrue(any("persistent" in query.lower() and "rag" in query.lower() for query in embedding_search.queries))
        self.assertTrue(any("지속적 위키" in query for query in embedding_search.queries))
        self.assertIn("concept:persistent-wiki", {item.page.id for item in result.related_pages})
        self.assertIsNotNone(answer_generator.last_context)
        self.assertIn("# User Question\n그거랑 RAG 차이는?", answer_generator.last_context.answer_context)
        self.assertIn("# Resolved Retrieval Question", answer_generator.last_context.answer_context)
        self.assertIn("Persistent Wiki", answer_generator.last_context.answer_context)

    def test_falls_back_to_web_search_when_internal_relevance_is_low(self) -> None:
        pages = [source_page("source:seed", "Seed Source")]
        web_search = FakeWebSearch(
            [
                WebSearchResult(
                    title="External RAG Reference",
                    url="https://example.com/rag",
                    snippet="RAG retrieves external knowledge and uses it to ground generated answers.",
                    score=0.91,
                )
            ]
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"Seed Source": 0.10}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            query_rewriter=FixedQueryRewriter("rag external knowledge"),
            web_search=web_search,
            min_internal_relevance_score=0.50,
        )

        result = use_case.execute("RAG가 뭐야?", workspace_id="ws_test")

        self.assertEqual(web_search.queries, ["rag external knowledge"])
        self.assertEqual(result.retrieval_summary.stop_reason, "web_search_fallback")
        self.assertEqual(result.traversal_paths, [])
        self.assertEqual(result.related_pages[0].page.page_type, "web")
        self.assertEqual(result.related_pages[0].page.markdown_uri, "https://example.com/rag")
        self.assertEqual(result.evidence_snippets[0].source_document_id, result.related_pages[0].page.id)
        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["web"])
        self.assertIn("external knowledge", result.evidence_snippets[0].text)
        self.assertIn("[1]", result.answer.content)
        self.assertTrue(result.web_search.requested)
        self.assertTrue(result.web_search.executed)
        self.assertEqual(result.web_search.result_count, 1)
        self.assertIsNone(result.web_search.error_code)

    def test_does_not_call_web_search_when_request_disallows_it(self) -> None:
        web_search = FakeWebSearch([])
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository([], []),
            embedding_search=ScoreSearch({}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            web_search=web_search,
            min_internal_relevance_score=0.50,
        )

        result = use_case.execute(
            "RAG가 뭐야?",
            workspace_id="ws_test",
            output_language="en",
            allow_web_search=False,
        )

        self.assertEqual(web_search.queries, [])
        self.assertNotEqual(result.retrieval_summary.stop_reason, "web_search_fallback")
        self.assertTrue(result.answer.content.startswith("The provided evidence"))
        self.assertFalse(result.web_search.requested)
        self.assertFalse(result.web_search.executed)
        self.assertEqual(result.web_search.result_count, 0)
        self.assertIsNone(result.web_search.error_code)

    def test_does_not_report_web_search_for_sufficient_internal_answer_without_adapter(self) -> None:
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository([source_page("source:seed", "Seed Source")], []),
            embedding_search=ScoreSearch({"Seed Source": 0.95}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            min_internal_relevance_score=0.50,
        )

        result = use_case.execute("Seed Source가 뭐야?", workspace_id="ws_test", allow_web_search=True)

        self.assertFalse(result.web_search.requested)
        self.assertFalse(result.web_search.executed)
        self.assertEqual(result.web_search.result_count, 0)
        self.assertIsNone(result.web_search.error_code)

    def test_reports_unavailable_when_low_relevance_requires_web_search_without_adapter(self) -> None:
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository([source_page("source:seed", "Seed Source")], []),
            embedding_search=ScoreSearch({"Seed Source": 0.10}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            min_internal_relevance_score=0.50,
        )

        result = use_case.execute("외부 정보가 뭐야?", workspace_id="ws_test", allow_web_search=True)

        self.assertTrue(result.web_search.requested)
        self.assertFalse(result.web_search.executed)
        self.assertEqual(result.web_search.result_count, 0)
        self.assertEqual(result.web_search.error_code, "web_search_unavailable")

    def test_web_search_failure_is_correlated_to_internal_result(self) -> None:
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository([source_page("source:seed", "Seed Source")], []),
            embedding_search=ScoreSearch({"Seed Source": 0.10}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            web_search=FailingWebSearch(),
            min_internal_relevance_score=0.50,
        )

        result = use_case.execute("RAG가 뭐야?", workspace_id="ws_test")

        self.assertTrue(result.web_search.requested)
        self.assertTrue(result.web_search.executed)
        self.assertEqual(result.web_search.result_count, 0)
        self.assertEqual(result.web_search.error_code, "web_search_failed")
        self.assertNotIn("provider detail must not reach", result.answer.content)

    def test_evaluator_web_route_returns_limitation_when_web_search_has_no_answer(self) -> None:
        for route in ("web_fallback", "internal_web_augmented"):
            for web_search in (FailingWebSearch(), FakeWebSearch([])):
                with self.subTest(route=route, web_search=type(web_search).__name__):
                    use_case = AnswerQueryUseCase(
                        wiki_repository=InMemoryWikiRepository([source_page("source:wiki", "Wiki Source")], []),
                        embedding_search=ScoreSearch({"Wiki Source": 0.95}),
                        text_search=EmptyTextSearch(),
                        answer_generator=RecordingAnswerGenerator("근거 없이 만든 외부 사실입니다. [1]"),
                        query_evaluator=FakeQueryEvaluator(
                            QueryEvaluation(
                                route=route,
                                evidence_relevance=0.0,
                                web_query="외부 사실",
                            )
                        ),
                        web_search=web_search,
                    )

                    result = use_case.execute("외부 사실을 알려줘.", workspace_id="ws_test")

                    self.assertNotIn("근거 없이 만든 외부 사실", result.answer.content)
                    self.assertTrue(result.web_search.executed)

    def test_query_evaluator_reviews_generated_answer_and_can_keep_internal_answer_when_seed_score_is_low(self) -> None:
        pages = [source_page("source:wiki", "LLM Wiki Source")]
        answer_generator = RecordingAnswerGenerator("index.md는 위키 페이지 카탈로그입니다. [1]")
        query_evaluator = FakeQueryEvaluator(
            QueryEvaluation(
                route="internal_supported",
                evidence_relevance=1.0,
                reason="정확한 내부 근거가 있습니다.",
            )
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"LLM Wiki Source": 0.0}),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:wiki.md": (
                        "---\ndocument_id: doc_wiki\n---\n\n"
                        "## Key Points\n"
                        "- index.md는 위키 페이지 카탈로그 역할을 하며 LLM이 쿼리 시 첫 번째로 참조하는 파일입니다. [B0022]\n"
                    )
                }
            ),
            query_evaluator=query_evaluator,
        )

        result = use_case.execute("index.md는 어떤 역할을 해?", workspace_id="ws_test")

        self.assertEqual(query_evaluator.calls[0][3], "no_relevant_seed")
        self.assertFalse(query_evaluator.calls[0][4])
        self.assertIn("index.md는 위키 페이지 카탈로그입니다.", query_evaluator.calls[0][2].content)
        self.assertEqual(result.retrieval_summary.stop_reason, "query_evaluator_internal_supported")
        self.assertIn("index.md는 위키 페이지 카탈로그입니다.", result.answer.content)
        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["B0022"])

    def test_query_evaluator_refuses_weak_internal_evidence_without_web_search(self) -> None:
        pages = [source_page("source:adjacent", "Adjacent Wiki Source")]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"Adjacent Wiki Source": 0.9}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator("망원경 설정은 이 문서에 있습니다. [1]"),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:adjacent.md": (
                        "---\ndocument_id: doc_adjacent\n---\n\n"
                        "이 문서는 내부 용어를 정리한 참고 자료입니다. [B0001]"
                    )
                }
            ),
            query_evaluator=FakeQueryEvaluator(
                QueryEvaluation(
                    route="unsupported",
                    evidence_relevance=0.2,
                    reason="질문과 인접한 내용만 있습니다.",
                )
            ),
        )

        result = use_case.execute(
            "망원경을 어떻게 설정하나요?",
            workspace_id="ws_test",
            output_language="ko",
            allow_web_search=False,
        )

        self.assertTrue(result.answer.content.startswith("제공된 근거에서 질문에 직접 답할 내용을 찾지 못했습니다."))
        self.assertNotIn("망원경 설정은 이 문서에 있습니다.", result.answer.content)
        self.assertEqual(result.retrieval_summary.stop_reason, "query_evaluator_unsupported")

    def test_weak_internal_evidence_reaches_web_fallback_when_web_is_enabled(self) -> None:
        page = source_page("source:adjacent", "Adjacent Wiki Source")
        web_search = FakeWebSearch(
            [
                WebSearchResult(
                    title="Telescope Setup",
                    url="https://example.com/telescope",
                    snippet="External setup instructions.",
                    score=0.9,
                )
            ]
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository([page], []),
            embedding_search=ScoreSearch({"Adjacent Wiki Source": 0.9}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator("망원경 설정은 이 문서에 있습니다. [1]"),
            markdown_reader=FakeMarkdownReader(
                {
                    page.markdown_uri: "---\ndocument_id: doc_adjacent\n---\n\n"
                    "내부 용어를 정리한 참고 자료입니다. [B0001]"
                }
            ),
            query_evaluator=FakeQueryEvaluator(
                QueryEvaluation(
                    route="web_fallback",
                    evidence_relevance=0.2,
                    reason="질문과 직접 관련 없는 내부 근거입니다.",
                    web_query="telescope setup",
                )
            ),
            web_search=web_search,
        )

        result = use_case.execute("망원경을 어떻게 설정하나요?", workspace_id="ws_test")

        self.assertEqual(web_search.queries, ["telescope setup"])
        self.assertEqual(result.retrieval_summary.stop_reason, "web_search_fallback")
        self.assertTrue(result.web_search.executed)

    def test_weak_internal_evidence_returns_grounded_refusal_when_web_is_disabled(self) -> None:
        page = source_page("source:adjacent", "Adjacent Wiki Source")
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository([page], []),
            embedding_search=ScoreSearch({"Adjacent Wiki Source": 0.9}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator("망원경 설정은 이 문서에 있습니다. [1]"),
            markdown_reader=FakeMarkdownReader(
                {
                    page.markdown_uri: "---\ndocument_id: doc_adjacent\n---\n\n"
                    "내부 용어를 정리한 참고 자료입니다. [B0001]"
                }
            ),
            query_evaluator=FakeQueryEvaluator(
                QueryEvaluation(
                    route="web_fallback",
                    evidence_relevance=0.2,
                    reason="질문과 직접 관련 없는 내부 근거입니다.",
                    web_query="telescope setup",
                )
            ),
        )

        result = use_case.execute(
            "망원경을 어떻게 설정하나요?",
            workspace_id="ws_test",
            allow_web_search=False,
            output_language="ko",
        )

        self.assertTrue(result.answer.content.startswith("제공된 근거에서 질문에 직접 답할 내용을 찾지 못했습니다."))
        self.assertNotIn("망원경 설정은 이 문서에 있습니다.", result.answer.content)

    def test_supported_internal_evidence_stays_internal_with_web_enabled(self) -> None:
        page = source_page("source:telescope", "Telescope Setup")
        web_search = FakeWebSearch([])
        answer_generator = RecordingAnswerGenerator("망원경은 초점 링을 돌려 초점을 맞춥니다. [1]")
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository([page], []),
            embedding_search=ScoreSearch({"Telescope Setup": 0.9}),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    page.markdown_uri: "---\ndocument_id: doc_telescope\n---\n\n"
                    "망원경은 초점 링을 돌려 초점을 맞춥니다. [B0001]"
                }
            ),
            query_evaluator=FakeQueryEvaluator(
                QueryEvaluation(
                    route="internal_supported",
                    evidence_relevance=0.2,
                    reason="내부 근거가 질문에 직접 답합니다.",
                )
            ),
            web_search=web_search,
        )

        result = use_case.execute(
            "망원경은 어떻게 초점을 맞추나요?",
            workspace_id="ws_test",
            allow_web_search=True,
        )

        self.assertEqual(web_search.queries, [])
        self.assertEqual(result.retrieval_summary.stop_reason, "no_frontier")
        self.assertIn("망원경은 초점 링을 돌려 초점을 맞춥니다.", result.answer.content)

    def test_query_evaluator_feedback_can_retry_answer_until_internal_supported(self) -> None:
        pages = [source_page("source:wiki", "LLM Wiki Source")]
        answer_generator = SequencedAnswerGenerator(
            [
                "초안 답변입니다. [1]",
                "evaluator 피드백을 반영한 개선 답변입니다. [1]",
            ]
        )
        query_evaluator = FakeQueryEvaluator(
            [
                QueryEvaluation(
                    route="revise_answer",
                    evidence_relevance=0.2,
                    reason="근거 문장을 충분히 사용하지 않았습니다.",
                    feedback="근거 문장을 직접 반영해 답변하세요.",
                ),
                QueryEvaluation(
                    route="internal_supported",
                    evidence_relevance=0.95,
                    reason="내부 근거로 충분히 답했습니다.",
                ),
            ]
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"LLM Wiki Source": 0.95}),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:wiki.md": (
                        "---\ndocument_id: doc_wiki\n---\n\n"
                        "## Key Points\n"
                        "- LLM Wiki Source는 내부 근거로 답변을 생성하는 데 사용됩니다. [B0001]\n"
                    )
                }
            ),
            query_evaluator=query_evaluator,
            query_evaluator_max_attempts=2,
        )

        result = use_case.execute("LLM Wiki Source는 어디에 사용돼?", workspace_id="ws_test")

        self.assertEqual(len(query_evaluator.calls), 2)
        self.assertEqual(len(answer_generator.contexts), 2)
        self.assertIn("근거 문장을 직접 반영해 답변하세요.", answer_generator.contexts[1].answer_context)
        self.assertIn("evaluator 피드백을 반영한 개선 답변입니다.", result.answer.content)

    def test_query_evaluator_returns_unsupported_when_revision_remains_unresolved(self) -> None:
        pages = [source_page("source:wiki", "LLM Wiki Source")]
        answer_generator = SequencedAnswerGenerator(["초안 답변입니다. [1]", "수정 답변입니다. [1]"])
        query_evaluator = FakeQueryEvaluator(
            [
                QueryEvaluation(
                    route="revise_answer",
                    evidence_relevance=0.8,
                    reason="인용이 주장과 맞지 않습니다.",
                    feedback="인용을 직접 근거와 일치시키세요.",
                ),
                QueryEvaluation(
                    route="revise_answer",
                    evidence_relevance=0.8,
                    reason="인용 문제가 남아 있습니다.",
                    feedback="인용을 다시 확인하세요.",
                ),
            ]
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"LLM Wiki Source": 0.95}),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:wiki.md": (
                        "---\ndocument_id: doc_wiki\n---\n\n"
                        "## Key Points\n"
                        "- 日本国 [B0001]\n"
                    )
                }
            ),
            query_evaluator=query_evaluator,
            query_evaluator_max_attempts=2,
        )

        result = use_case.execute(
            "この文書は何を説明していますか？",
            workspace_id="ws_test",
            output_language="document",
        )

        self.assertEqual(len(query_evaluator.calls), 2)
        self.assertEqual(len(answer_generator.contexts), 2)
        self.assertIn("인용을 직접 근거와 일치시키세요.", answer_generator.contexts[1].answer_context)
        self.assertEqual(result.retrieval_summary.stop_reason, "query_evaluator_unresolved")
        self.assertNotIn("수정 답변입니다.", result.answer.content)
        self.assertIn("提供された根拠", result.answer.content)

    def test_document_language_uses_question_when_han_text_is_ambiguous(self) -> None:
        self.assertEqual(_fallback_language("document", "日本国", "日本国について"), "ja")
        self.assertEqual(_fallback_language("document", "中国市场", "中国市场是什么？"), "zh")
        self.assertEqual(_fallback_language("document", "中国市场", "中国市場とは何ですか？"), "zh")
        self.assertEqual(_fallback_language("document", "日本市場", "日本市場とは何ですか？"), "ja")
        self.assertEqual(_fallback_language("document", "한국어 문서", "質問"), "ko")
        self.assertEqual(_fallback_language("document", "こんにちは", "질문"), "ja")

    def test_query_evaluator_can_request_web_fallback_after_reviewing_answer(self) -> None:
        pages = [source_page("source:wiki", "LLM Wiki Source")]
        answer_generator = RecordingAnswerGenerator()
        web_search = FakeWebSearch(
            [
                WebSearchResult(
                    title="External Tool Reference",
                    url="https://example.com/operator",
                    snippet="External tools provide capabilities beyond the internal Wiki.",
                    score=0.9,
                )
            ]
        )
        query_evaluator = FakeQueryEvaluator(
            QueryEvaluation(
                route="web_fallback",
                evidence_relevance=0.0,
                web_query="external tool overview",
                reason="내부 근거가 없습니다.",
            )
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"LLM Wiki Source": 0.95}),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            query_evaluator=query_evaluator,
            web_search=web_search,
        )

        result = use_case.execute("외부 도구는 뭐야?", workspace_id="ws_test")

        self.assertEqual(len(query_evaluator.calls), 1)
        self.assertTrue(query_evaluator.calls[0][4])
        self.assertIn("테스트 답변입니다.", query_evaluator.calls[0][2].content)
        self.assertEqual(web_search.queries, ["external tool overview"])
        self.assertEqual(result.retrieval_summary.stop_reason, "web_search_fallback")
        self.assertEqual(result.related_pages[0].page.page_type, "web")
        self.assertIsNotNone(answer_generator.last_context)
        self.assertIn("# Web Fallback Answer Policy", answer_generator.last_context.answer_context)
        self.assertIn("Use web evidence as the grounding evidence", answer_generator.last_context.answer_context)
        self.assertNotIn("For unsupported questions, do not explain", answer_generator.last_context.answer_context)

    def test_query_evaluator_can_request_internal_web_augmented_after_reviewing_answer(self) -> None:
        pages = [source_page("source:wiki", "LLM Wiki Source")]
        web_title = "External Deployment Guide"
        web_snippet = "External deployment evidence explains implementation steps."
        web_search = FakeWebSearch(
            [
                WebSearchResult(
                    title=web_title,
                    url="https://example.com/operator-deploy",
                    snippet=web_snippet,
                    content="External deployment evidence explains implementation steps with web-only details.",
                    score=0.88,
                )
            ]
        )
        generated_answer = (
            "외부 구현 절차는 웹 근거를 사용해 설명합니다. [2]\n"
            "내부 근거는 대상 시스템의 개념을 설명합니다. [1]"
        )
        answer_generator = RecordingAnswerGenerator(
            generated_answer
        )
        query_evaluator = FakeQueryEvaluator(
            QueryEvaluation(
                route="internal_web_augmented",
                evidence_relevance=0.7,
                web_query="external deployment details",
                reason="내부 주제는 맞지만 외부 구현 근거가 필요합니다.",
            )
        )
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"LLM Wiki Source": 0.95}),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:wiki.md": (
                        "---\ndocument_id: doc_wiki\n---\n\n"
                        "Persistent Wiki는 LLM이 기존 위키를 업데이트하며 지식을 축적하는 구조입니다. [B0001]\n"
                    )
                }
            ),
            query_evaluator=query_evaluator,
            web_search=web_search,
        )

        result = use_case.execute("그거를 외부 배포 방식으로 운영하려면 어떻게 해?", workspace_id="ws_test")

        self.assertEqual(len(query_evaluator.calls), 1)
        self.assertTrue(query_evaluator.calls[0][4])
        self.assertIn("외부 구현 절차는 웹 근거를 사용해 설명합니다.", query_evaluator.calls[0][2].content)
        self.assertEqual(web_search.queries, ["external deployment details"])
        self.assertEqual(result.retrieval_summary.stop_reason, "internal_web_augmented")
        self.assertIn("web", {item.page.page_type for item in result.related_pages})
        self.assertIn("source", {item.page.page_type for item in result.related_pages})
        self.assertTrue(any(snippet.source_block_ids == ["web"] for snippet in result.evidence_snippets))
        self.assertIn("외부 구현 절차는 웹 근거를 사용해 설명합니다. [1]", result.answer.content)
        self.assertIn("내부 근거는 대상 시스템의 개념을 설명합니다. [2]", result.answer.content)
        self.assertIsNotNone(answer_generator.last_context)
        self.assertIn("# Internal-Web Augmented Answer Policy", answer_generator.last_context.answer_context)
        self.assertIn("Use web evidence to answer the external implementation", answer_generator.last_context.answer_context)
        self.assertIn("Do not answer as an unsupported/refusal response", answer_generator.last_context.answer_context)
        self.assertIn("web evidence was used because the requested external details", answer_generator.last_context.answer_context)
        self.assertNotIn("For unsupported questions, do not explain", answer_generator.last_context.answer_context)
        self.assertIn("Give a constructive answer by combining the retrieved Wiki evidence", answer_generator.last_context.answer_context)
        self.assertIn(web_title, answer_generator.last_context.answer_context)
        self.assertIn(web_snippet, answer_generator.last_context.answer_context)

    def test_direct_concept_name_match_can_answer_without_relevant_source_seed(self) -> None:
        pages = [
            source_page("source:solar-system", "Solar System Source"),
            concept_page("concept:sun", "태양"),
            concept_page("concept:solar-system", "태양계"),
        ]
        links = [
            WikiPageLink(
                from_page_id="source:solar-system",
                to_page_id="concept:sun",
                link_type="source_mentions_concept",
                confidence=0.97,
            )
        ]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, links),
            embedding_search=ScoreSearch(
                {
                    "Solar System Source": 0.0,
                    "태양": 0.20,
                    "태양계": 0.30,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:solar-system.md": "---\ndocument_id: doc_solar\n---\n\n태양계는 태양과 행성으로 구성됩니다. [B0001]",
                    "s3://test/concept:sun.md": "---\ntype: concept\nsources: doc_solar\n---\n\n태양은 태양계의 중심에 있는 별입니다. [B0002]",
                    "s3://test/concept:solar-system.md": "---\ntype: concept\nsources: doc_solar\n---\n\n태양계는 태양, 행성, 위성 등으로 이루어진 천체 체계입니다. [B0003]",
                }
            ),
            query_rewriter=FixedQueryRewriter("태양"),
            min_internal_relevance_score=0.50,
        )

        result = use_case.execute("태양이 뭐야?", workspace_id="ws_test")

        self.assertEqual(result.retrieval_summary.stop_reason, "concept_direct_match")
        self.assertIn("concept:sun", {item.page.id for item in result.related_pages})
        self.assertIn("source_mentions_concept", {edge.link_type for edge in result.graph_context.edges})
        self.assertEqual(result.traversal_paths[0].stop_reason, "concept_direct_match")
        self.assertEqual(result.traversal_paths[0].nodes, ["source:solar-system", "concept:sun"])
        self.assertIn("태양은 태양계의 중심", result.evidence_snippets[0].text)
        self.assertEqual(result.evidence_snippets[0].source_document_id, "doc_solar")
        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["B0002"])
        self.assertIn("[1]", result.answer.content)

    def test_focus_concepts_are_used_as_graph_seed_pages(self) -> None:
        pages = [
            source_page("source:wiki", "Wiki Source"),
            concept_page("concept:llm-wiki", "LLM Wiki"),
        ]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch(
                {
                    "Wiki Source": 0.20,
                    "LLM Wiki": 0.95,
                }
            ),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:wiki.md": "---\ndocument_id: doc_wiki\n---\n\nSource summary [B0001]",
                    "s3://test/concept:llm-wiki.md": "---\ntype: concept\nsources: doc_wiki\n---\n\nLLM Wiki는 위키 기반 지식 구조입니다. [B0002]",
                }
            ),
        )

        result = use_case.execute("LLM Wiki가 뭐야?", workspace_id="ws_test")

        related = {item.page.id: item for item in result.related_pages}
        self.assertIn("concept:llm-wiki", related)
        self.assertEqual(related["concept:llm-wiki"].role, "focus_concept")
        self.assertEqual(related["concept:llm-wiki"].depth, 0)

    def test_evidence_uses_only_sentences_with_source_block_refs(self) -> None:
        pages = [source_page("source:seed", "Seed Source")]
        answer_generator = RecordingAnswerGenerator()
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"Seed Source": 0.95}),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:seed.md": (
                        "---\ndocument_id: doc_seed\n---\n\n"
                        "요약 문장은 citation이 없어서 근거가 되면 안 됩니다.\n\n"
                        "원본에 연결된 근거 문장입니다. [B0005]"
                    )
                }
            ),
        )

        result = use_case.execute("원본에 연결된 근거는?", workspace_id="ws_test")

        self.assertEqual(len(result.evidence_snippets), 1)
        self.assertEqual(result.evidence_snippets[0].source_document_id, "doc_seed")
        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["B0005"])
        self.assertEqual(result.evidence_snippets[0].text, "원본에 연결된 근거 문장입니다.")
        self.assertNotIn("citation이 없어서", answer_generator.last_context.answer_context)

    def test_evidence_prefers_matching_key_point_bullet_over_entire_section(self) -> None:
        pages = [source_page("source:wiki", "LLM Wiki Source")]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"LLM Wiki Source": 0.95}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:wiki.md": (
                        "---\ndocument_id: doc_wiki\n---\n\n"
                        "## Key Points\n"
                        "- 기존 RAG 시스템과의 차이점: 지속적 축적 vs 일회성 검색 [B0001]\n"
                        "- 3계층 아키텍처: 원시 문서, 위키, 스키마 [B0002]\n"
                        "- Obsidian Web Clipper는 웹 기사를 마크다운으로 변환하는 브라우저 확장입니다. [B0003]\n"
                    )
                }
            ),
        )

        result = use_case.execute("Obsidian Web Clipper 역할", workspace_id="ws_test")

        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["B0003"])
        self.assertIn("Obsidian Web Clipper", result.evidence_snippets[0].text)
        self.assertNotIn("기존 RAG", result.evidence_snippets[0].text)
        self.assertNotIn("3계층", result.evidence_snippets[0].text)

    def test_evidence_keeps_aggregate_and_includes_atomic_units_inside_selected_page(self) -> None:
        pages = [source_page("source:wiki", "LLM Wiki Source")]
        answer_generator = RecordingAnswerGenerator("유지관리 작업입니다. [1] Ingest 설명입니다. [2] Query 설명입니다. [3] Lint 설명입니다. [4]")
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"LLM Wiki Source": 0.95}),
            text_search=EmptyTextSearch(),
            answer_generator=answer_generator,
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:wiki.md": (
                        "---\ndocument_id: doc_wiki\n---\n\n"
                        "## Key Points\n"
                        "- Ingest, Query, Lint 작업으로 위키 유지 관리 [B0017, B0018, B0019]\n\n"
                        "## Section Candidates\n"
                        "- Ingest Operation - 새 문서를 처리하고 위키를 업데이트하는 작업 [B0017]\n"
                        "- Query Operation - 위키에 질문하고 답변을 생성하는 작업 [B0018]\n"
                        "- Lint Operation - 위키의 건강 상태를 점검하는 작업 [B0019]\n"
                    )
                }
            ),
        )

        result = use_case.execute("ingest, query, lint는 각각 어떤 역할을 해?", workspace_id="ws_test")

        self.assertIsNotNone(answer_generator.last_context)
        evidence_refs = [snippet.source_block_ids for snippet in answer_generator.last_context.evidence_snippets[:4]]
        self.assertIn(["B0017", "B0018", "B0019"], evidence_refs)
        self.assertIn(["B0017"], evidence_refs)
        self.assertIn(["B0018"], evidence_refs)
        self.assertIn(["B0019"], evidence_refs)
        self.assertEqual(evidence_refs, [snippet.source_block_ids for snippet in result.evidence_snippets[:4]])

    def test_evidence_excludes_core_concept_link_section(self) -> None:
        pages = [source_page("source:wiki", "LLM Wiki Source")]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"LLM Wiki Source": 0.95}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:wiki.md": (
                        "---\ndocument_id: doc_wiki\n---\n\n"
                        "## Core Concepts\n"
                        "- [[persistent-wiki|Persistent Wiki]] [B0001]\n\n"
                        "## Key Points\n"
                        "- Persistent Wiki는 기존 RAG 시스템과 달리 지식을 지속적으로 축적합니다. [B0002]\n"
                    )
                }
            ),
        )

        result = use_case.execute("Persistent Wiki RAG 차이", workspace_id="ws_test")

        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["B0002"])
        self.assertIn("지속적으로 축적", result.evidence_snippets[0].text)
        self.assertNotIn("[[persistent-wiki", result.evidence_snippets[0].text)

    def test_evidence_can_use_observation_episode_before_concept_link(self) -> None:
        pages = [source_page("source:wiki", "LLM Wiki Source")]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"LLM Wiki Source": 0.95}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:wiki.md": (
                        "---\ndocument_id: doc_wiki\n---\n\n"
                        "## Core Concepts\n"
                        "- [[llm-wiki|LLM Wiki]] [B0001]\n\n"
                        "## Observations\n"
                        "- O001 (qa_episode) LLM Wiki와 RAG 차이 질문 / query: LLM Wiki는 RAG랑 뭐가 달라? / "
                        "summary: LLM Wiki는 대화에서 얻은 지식을 위키 페이지로 누적하고, RAG는 질문 시점에 문서를 검색해 답한다. [B0002, B0003]\n"
                    )
                }
            ),
        )

        result = use_case.execute("LLM Wiki랑 RAG 차이는?", workspace_id="ws_test")

        self.assertEqual(result.evidence_snippets[0].source_block_ids, ["B0002", "B0003"])
        self.assertIn("qa_episode", result.evidence_snippets[0].text)
        self.assertIn("RAG", result.evidence_snippets[0].text)
        self.assertNotIn("[[llm-wiki", result.evidence_snippets[0].text)

    def test_source_structure_sections_boost_source_retrieval(self) -> None:
        pages = [
            source_page("source:qmd", "Low Base QMD Source"),
            source_page("source:other", "Other Source"),
        ]
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository(pages, []),
            embedding_search=ScoreSearch({"Low Base QMD Source": 0.0, "Other Source": 0.0}),
            text_search=QueryContainsSearch(),
            answer_generator=RecordingAnswerGenerator(),
            markdown_reader=FakeMarkdownReader(
                {
                    "s3://test/source:qmd.md": (
                        "---\ndocument_id: doc_qmd\n---\n\n"
                        "## Summary\n요약에는 검색어가 없습니다.\n\n"
                        "## Section Candidates\n- qmd - 로컬 마크다운 검색 엔진입니다. [B0025]\n"
                    ),
                    "s3://test/source:other.md": "---\ndocument_id: doc_other\n---\n\n## Summary\n다른 문서입니다. [B0001]",
                }
            ),
            source_candidate_limit=1,
        )

        result = use_case.execute("qmd", workspace_id="ws_test")

        self.assertEqual(result.related_pages[0].page.id, "source:qmd")

    def test_source_structure_sections_match_narrow_question_intent(self) -> None:
        cases = [
            ("Categories", "과학개념 추출해줘", "과학 개념"),
            ("Core Concepts", "응결이 뭐야?", "응결"),
            ("Section Candidates", "강수 과정 섹션이 있어?", "강수 과정"),
            ("Mentions", "지반이 언급된 적이 있나?", "지반"),
        ]
        for section_name, question, section_content in cases:
            with self.subTest(section_name=section_name):
                pages = [
                    source_page("source:target", "Target Source"),
                    source_page("source:other", "Other Source"),
                ]
                markdown_reader = FakeMarkdownReader(
                    {
                        "s3://test/source:target.md": (
                            "---\ndocument_id: doc_target\n---\n\n"
                            f"## {section_name}\n- {section_content} 범위에서 검색되어야 하는 항목입니다. [B0001]\n"
                        ),
                        "s3://test/source:other.md": (
                            "---\ndocument_id: doc_other\n---\n\n"
                            f"## {section_name}\n- 관련 없는 비교 항목입니다. [B0002]\n"
                        ),
                    }
                )
                use_case = AnswerQueryUseCase(
                    wiki_repository=InMemoryWikiRepository(pages, []),
                    embedding_search=SourceStructureIntentSearch(),
                    text_search=EmptyTextSearch(),
                    answer_generator=RecordingAnswerGenerator(),
                    markdown_reader=markdown_reader,
                    source_candidate_limit=1,
                )

                result = use_case.execute(question, workspace_id="ws_test")

                self.assertEqual(result.related_pages[0].page.id, "source:target")

    def test_stops_traversal_at_configured_max_depth(self) -> None:
        pages = [
            source_page("source:seed", "Seed Source"),
            concept_page("concept:one", "Concept One"),
            concept_page("concept:two", "Concept Two"),
            concept_page("concept:three", "Concept Three"),
            concept_page("concept:four", "Concept Four"),
        ]
        links = [
            WikiPageLink("source:seed", "concept:one", "source_mentions_concept", confidence=0.99),
            WikiPageLink("concept:one", "concept:two", "concept_related_to", confidence=0.99),
            WikiPageLink("concept:two", "concept:three", "concept_related_to", confidence=0.99),
            WikiPageLink("concept:three", "concept:four", "concept_related_to", confidence=0.99),
        ]
        graph_context, _paths, stop_reason = TraverseWikiGraphUseCase(max_depth=2).execute(
            pages_by_id={page.id: page for page in pages},
            links=links,
            seed_page_ids=["source:seed"],
            node_scores={page.id: 0.95 for page in pages},
        )

        related_ids = {item.page.id for item in graph_context.nodes}
        self.assertIn("concept:two", related_ids)
        self.assertNotIn("concept:three", related_ids)
        self.assertNotIn("concept:four", related_ids)
        self.assertEqual(max(item.depth for item in graph_context.nodes), 2)
        self.assertEqual(stop_reason, "max_depth")

    def test_traverses_materialized_lint_relations_and_excludes_non_materialized_links(self) -> None:
        relation_types = [
            "source_mentions_concept",
            "concept_related_to",
            "part_of",
            "child_of",
            "uses_or_depends_on",
            "contrasts_with",
            "supports_or_enables",
        ]
        pages = [source_page("source:seed", "Seed Source")]
        links = []
        for index, relation_type in enumerate(relation_types, start=1):
            target = concept_page(f"concept:{index}", f"Concept {index}")
            pages.append(target)
            links.append(WikiPageLink("source:seed", target.id, relation_type, confidence=0.99))
        pages.extend(
            [
                concept_page("concept:related-evidence", "Related Evidence"),
                concept_page("concept:unknown", "Unknown Concept"),
            ]
        )
        links.extend(
            [
                WikiPageLink("source:seed", "concept:related-evidence", "related_evidence", confidence=0.99),
                WikiPageLink("source:seed", "concept:unknown", "unknown", confidence=0.99),
            ]
        )

        graph_context, paths, _stop_reason = TraverseWikiGraphUseCase().execute(
            pages_by_id={page.id: page for page in pages},
            links=links,
            seed_page_ids=["source:seed"],
            node_scores={page.id: 1.0 for page in pages},
        )

        self.assertEqual(
            {edge.link_type for edge in graph_context.edges},
            set(relation_types),
        )
        self.assertNotIn(
            ("source:seed", "concept:related-evidence", "related_evidence"),
            {(edge.from_page_id, edge.to_page_id, edge.link_type) for edge in graph_context.edges},
        )
        self.assertNotIn(
            ("source:seed", "concept:unknown", "unknown"),
            {(edge.from_page_id, edge.to_page_id, edge.link_type) for edge in graph_context.edges},
        )
        self.assertEqual(
            {item.page.id for item in graph_context.nodes},
            {"source:seed", *(f"concept:{index}" for index in range(1, 8))},
        )
        self.assertFalse(
            any(
                excluded_id in path.nodes
                for path in paths
                for excluded_id in ("concept:related-evidence", "concept:unknown")
            )
        )

    def test_reports_no_frontier_when_graph_ends_at_max_depth(self) -> None:
        pages = [
            source_page("source:seed", "Seed Source"),
            concept_page("concept:one", "Concept One"),
        ]

        _graph_context, _paths, stop_reason = TraverseWikiGraphUseCase(max_depth=1).execute(
            pages_by_id={page.id: page for page in pages},
            links=[WikiPageLink("source:seed", "concept:one", "source_mentions_concept")],
            seed_page_ids=["source:seed"],
            node_scores={page.id: 0.95 for page in pages},
        )

        self.assertEqual(stop_reason, "no_frontier")

    def test_stops_traversal_below_initial_best_seed_floor(self) -> None:
        pages = [
            source_page("source:seed", "Seed Source"),
            concept_page("concept:near", "Near Concept"),
            concept_page("concept:drift", "Drift Concept"),
        ]
        graph_context, _paths, _stop_reason = TraverseWikiGraphUseCase(
            relative_score_floor=0.95,
        ).execute(
            pages_by_id={page.id: page for page in pages},
            links=[
                WikiPageLink(
                    "source:seed",
                    "concept:near",
                    "source_mentions_concept",
                ),
                WikiPageLink(
                    "concept:near",
                    "concept:drift",
                    "concept_related_to",
                ),
            ],
            seed_page_ids=["source:seed"],
            node_scores={
                "source:seed": 1.0,
                "concept:near": 0.96,
                "concept:drift": 0.94,
            },
        )

        related_ids = {item.page.id for item in graph_context.nodes}
        self.assertIn("concept:near", related_ids)
        self.assertNotIn("concept:drift", related_ids)

    def test_rejects_blank_question(self) -> None:
        use_case = AnswerQueryUseCase(
            wiki_repository=InMemoryWikiRepository([], []),
            embedding_search=ScoreSearch({}),
            text_search=EmptyTextSearch(),
            answer_generator=RecordingAnswerGenerator(),
        )

        with self.assertRaises(InvalidQuestionError):
            use_case.execute("   ", workspace_id="ws_test")


if __name__ == "__main__":
    unittest.main()
