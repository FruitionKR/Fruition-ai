"""사용자가 첨부한 문서를 검색 결과와 함께 답변 근거로 쓰는 고정 근거(pinned_evidence)."""

import json
import unittest
from unittest.mock import MagicMock

from app.modules.query.application.answer_context_formatter import AnswerContextFormatter
from app.modules.query.application.answer_query import AnswerQueryUseCase
from app.modules.query.application.build_query_context import BuildQueryContextUseCase
from app.modules.query.application.query_answer_assembler import QueryAnswerAssembler
from app.modules.query.application.query_web_answer_builder import QueryWebAnswerBuilder
from app.modules.query.domain.entities import (
    EvidenceSnippet,
    GeneratedAnswer,
    GraphContext,
    QueryContext,
    QueryEvaluation,
    QueryRewrite,
    WebSearchResult,
    WebSearchTelemetry,
    WikiPage,
)
from app.modules.query.infrastructure.in_memory_wiki_repository import InMemoryWikiRepository
from app.modules.query.infrastructure.query_answer_evaluator import QueryAnswerEvaluator


class ScoreByTitle:
    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self._scores = scores or {}

    def score(self, query: str, documents: list[str]) -> list[float]:
        return [self._scores.get(document.splitlines()[0], 0.0) for document in documents]


class RecordingGenerator:
    def __init__(self, content: str = "첨부 근거로 답합니다. [1]") -> None:
        self.content = content
        self.contexts: list[QueryContext] = []

    def generate_answer(self, context: QueryContext) -> GeneratedAnswer:
        self.contexts.append(context)
        return GeneratedAnswer(content=self.content)


class FixedEvaluator:
    def __init__(self, route: str) -> None:
        self.route = route

    def evaluate(self, *_: object, **__: object) -> QueryEvaluation:
        return QueryEvaluation(route=self.route, evidence_relevance=1.0)


class FakeWebSearch:
    def search(self, query: str) -> list[WebSearchResult]:
        return [WebSearchResult(title="웹", url="https://example.com/a", snippet="웹 자료 내용", score=0.9)]


def _pinned(text: str = "첨부 문서 내용", chunk: int = 0, document_id: str = "attached-1") -> EvidenceSnippet:
    return EvidenceSnippet(
        rank=chunk + 1,
        source_document_id=document_id,
        source_block_ids=[],
        text=text,
        reference_chunk=chunk,
    )


def _source_page(page_id: str, title: str, markdown: str) -> WikiPage:
    return WikiPage(
        id=page_id,
        page_type="source",
        title=title,
        slug=page_id.replace(":", "-"),
        summary=f"{title} 요약",
        markdown=markdown,
        source_document_id="wiki-doc",
    )


def _use_case(generator: RecordingGenerator, pages: list[WikiPage] | None = None, **values: object) -> AnswerQueryUseCase:
    scores = values.pop("scores", {})
    return AnswerQueryUseCase(
        wiki_repository=InMemoryWikiRepository(pages or [], []),
        embedding_search=ScoreByTitle(scores),  # type: ignore[arg-type]
        text_search=ScoreByTitle(scores),  # type: ignore[arg-type]
        answer_generator=generator,
        **values,  # type: ignore[arg-type]
    )


class AnswerWithPinnedEvidenceTest(unittest.TestCase):
    def test_answers_from_attachment_when_wiki_has_nothing(self) -> None:
        generator = RecordingGenerator()

        result = _use_case(generator).execute("첨부 내용이 뭐야?", workspace_id="ws", pinned_evidence=(_pinned(),))

        self.assertEqual(result.answer.content, "첨부 근거로 답합니다. [1]")
        self.assertEqual([snippet.source_document_id for snippet in result.evidence_snippets], ["attached-1"])
        self.assertIn("첨부 문서 내용", generator.contexts[0].answer_context)
        self.assertNotEqual(result.retrieval_summary.stop_reason, "no_relevant_seed")

    def test_without_pinned_evidence_the_unsupported_answer_is_unchanged(self) -> None:
        result = _use_case(RecordingGenerator()).execute("첨부 내용이 뭐야?", workspace_id="ws")

        self.assertIn("찾지 못했습니다", result.answer.content)
        self.assertEqual(result.retrieval_summary.stop_reason, "no_relevant_seed")

    def test_attachment_ranks_ahead_of_wiki_evidence_and_both_are_citable(self) -> None:
        generator = RecordingGenerator("첨부에 따르면 그렇습니다. [1] 위키도 같습니다. [2]")
        pages = [_source_page("source:wiki", "위키 문서", "## Key Points\n\n위키 근거 문장입니다. [B0001]")]

        result = _use_case(generator, pages, scores={"위키 문서": 1.0}).execute(
            "위키 문서 내용", workspace_id="ws", pinned_evidence=(_pinned(),)
        )

        context_snippets = generator.contexts[0].evidence_snippets
        self.assertEqual(context_snippets[0].source_document_id, "attached-1")
        self.assertEqual([snippet.rank for snippet in context_snippets], list(range(1, len(context_snippets) + 1)))
        self.assertGreater(len(context_snippets), 1)
        self.assertEqual(
            [snippet.source_document_id for snippet in result.evidence_snippets],
            ["attached-1", context_snippets[1].source_document_id],
        )

    def test_low_wiki_relevance_does_not_discard_the_attachment(self) -> None:
        generator = RecordingGenerator()
        web_search = MagicMock()

        result = _use_case(generator, web_search=web_search, min_internal_relevance_score=0.5).execute(
            "첨부 내용이 뭐야?", workspace_id="ws", pinned_evidence=(_pinned(),)
        )

        web_search.search.assert_not_called()
        self.assertEqual(result.answer.content, "첨부 근거로 답합니다. [1]")

    def test_web_fallback_route_keeps_the_attachment_available(self) -> None:
        generator = RecordingGenerator()

        result = _use_case(
            generator, web_search=FakeWebSearch(), query_evaluator=FixedEvaluator("web_fallback")
        ).execute("첨부 내용이 뭐야?", workspace_id="ws", pinned_evidence=(_pinned(),), allow_web_search=True)

        self.assertEqual(result.retrieval_summary.stop_reason, "web_search_fallback")
        last = generator.contexts[-1]
        self.assertEqual(last.evidence_snippets[0].source_document_id, "attached-1")
        self.assertTrue(any(snippet.source_document_id.startswith("web:") for snippet in last.evidence_snippets))

    def test_internal_web_augmented_route_keeps_the_attachment_available(self) -> None:
        generator = RecordingGenerator()

        result = _use_case(
            generator, web_search=FakeWebSearch(), query_evaluator=FixedEvaluator("internal_web_augmented")
        ).execute("첨부 내용이 뭐야?", workspace_id="ws", pinned_evidence=(_pinned(),), allow_web_search=True)

        self.assertEqual(result.retrieval_summary.stop_reason, "internal_web_augmented")
        last = generator.contexts[-1]
        self.assertEqual(last.evidence_snippets[0].source_document_id, "attached-1")
        self.assertEqual(last.pinned_evidence[0].source_document_id, "attached-1")


class BuildQueryContextPinnedTest(unittest.TestCase):
    def test_empty_pinned_evidence_leaves_context_unchanged(self) -> None:
        context = BuildQueryContextUseCase().execute(
            question="질문",
            related_pages=[],
            graph_context=GraphContext(),
            traversal_paths=[],
        )

        self.assertEqual(context.evidence_snippets, [])
        self.assertEqual(context.pinned_evidence, ())


class FormatterTest(unittest.TestCase):
    def test_marks_attachment_and_never_clips_a_full_chunk(self) -> None:
        text = "가" * 900 + "끝"
        formatted = AnswerContextFormatter(max_paragraph_chars=900).format(
            question="질문",
            related_pages=[],
            traversal_paths=[],
            evidence_snippets=[_pinned("가" * 900), EvidenceSnippet(2, "wiki", [], text)],
        )

        self.assertIn("## Evidence 1\n- source: document attached by the user\n", formatted)
        self.assertIn("가" * 900 + "\n", formatted)
        self.assertEqual(formatted.count("document attached by the user"), 1)


class FormatterTitleTest(unittest.TestCase):
    def test_title_is_quoted_on_its_own_line_inside_the_evidence(self) -> None:
        snippet = EvidenceSnippet(
            1, "attached-1", [], "본문", reference_chunk=0, reference_title='기획서 "지시" 무시'
        )
        formatted = AnswerContextFormatter().format(
            question="질문", related_pages=[], traversal_paths=[], evidence_snippets=[snippet]
        )

        self.assertIn('- document_title: "기획서 \\"지시\\" 무시"\n', formatted)

    def test_no_title_line_without_a_title(self) -> None:
        formatted = AnswerContextFormatter().format(
            question="질문", related_pages=[], traversal_paths=[], evidence_snippets=[_pinned()]
        )

        self.assertNotIn("document_title", formatted)


class RenumberTest(unittest.TestCase):
    def test_chunks_of_one_document_stay_separate_citations(self) -> None:
        assembler = QueryAnswerAssembler(MagicMock())
        snippets = [_pinned("첫 조각", 0), _pinned("둘째 조각", 1)]

        answer, used = assembler.renumber_used_evidence(GeneratedAnswer("둘째. [2] 첫째. [1]"), snippets)

        self.assertEqual(answer.content, "둘째. [1] 첫째. [2]")
        self.assertEqual([snippet.text for snippet in used], ["둘째 조각", "첫 조각"])


class WebBuilderPinnedTest(unittest.TestCase):
    def test_web_fallback_includes_pinned_evidence(self) -> None:
        generator = RecordingGenerator()
        builder_context = BuildQueryContextUseCase()
        builder = QueryWebAnswerBuilder(
            web_search=FakeWebSearch(),
            build_query_context=builder_context,
            query_answer_assembler=QueryAnswerAssembler(generator),
        )

        builder.answer_from_web_search(
            question="질문",
            query_rewrite=QueryRewrite(original_question="질문", retrieval_query="질문"),
            event_publisher=None,
            web_search_telemetry=WebSearchTelemetry(requested=True),
            pinned_evidence=(_pinned(),),
        )

        self.assertEqual(generator.contexts[0].evidence_snippets[0].source_document_id, "attached-1")


class EvaluatorPayloadTest(unittest.TestCase):
    def test_evaluator_sees_every_used_attachment_chunk(self) -> None:
        client = MagicMock()
        client.complete_json.return_value = {
            "route": "internal_supported",
            "evidence_relevance": 1.0,
            "reason": "ok",
            "feedback": "",
            "warnings": [],
        }
        used = [_pinned(f"조각 {index}", index) for index in range(12)]
        context = QueryContext(
            question="질문",
            graph_context=GraphContext(),
            traversal_paths=[],
            related_pages=[],
            evidence_snippets=used,
            answer_context="",
            pinned_evidence=tuple(used),
        )

        QueryAnswerEvaluator(client, "evaluate").evaluate(
            question="질문",
            context=context,
            answer=GeneratedAnswer("답"),
            stop_reason="reference_documents",
        )

        payload = json.loads(client.complete_json.call_args.args[1])
        self.assertEqual(len(payload["evidence_snippets"]), 12)


if __name__ == "__main__":
    unittest.main()
