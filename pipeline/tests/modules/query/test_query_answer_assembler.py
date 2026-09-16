import re
import unittest

from app.modules.query.application.query_answer_assembler import QueryAnswerAssembler
from app.modules.query.domain.entities import EvidenceSnippet, GeneratedAnswer, GraphContext, QueryContext


class FixedAnswerGenerator:
    def __init__(self, content: str) -> None:
        self._content = content

    def generate_answer(self, context: QueryContext) -> GeneratedAnswer:
        return GeneratedAnswer(content=self._content)


def query_context(evidence_snippets: list[EvidenceSnippet]) -> QueryContext:
    return QueryContext(
        question="질문",
        graph_context=GraphContext(),
        traversal_paths=[],
        related_pages=[],
        evidence_snippets=evidence_snippets,
        answer_context="질문",
    )


class QueryAnswerAssemblerTest(unittest.TestCase):
    def test_empty_evidence_cannot_yield_citations(self) -> None:
        assembler = QueryAnswerAssembler(FixedAnswerGenerator("답변입니다. [1]"))

        answer, evidence_snippets = assembler.generate_supported_answer(query_context([]))

        self.assertNotRegex(answer.content, r"\[\d+(?:\s*,\s*\d+)*\]")
        self.assertEqual(evidence_snippets, [])

    def test_block_refs_are_removed_from_answer_body(self) -> None:
        """답변 본문 인용 계약은 숫자 rank다. 위키 인라인 앵커가 새어 나오면 지운다."""
        evidence_snippets = [
            EvidenceSnippet(rank=1, source_document_id="chatdoc_abc", source_block_ids=["B0003"], text="근거"),
        ]
        assembler = QueryAnswerAssembler(
            FixedAnswerGenerator("역색인을 씁니다. [chatdoc_abc:B0003] 그리고 [1]")
        )

        answer, _ = assembler.generate_supported_answer(query_context(evidence_snippets))

        self.assertNotIn("chatdoc_abc", answer.content)
        self.assertNotIn("B0003", answer.content)
        self.assertIn("[1]", answer.content)

    def test_block_refs_are_removed_from_unsupported_answer(self) -> None:
        """generate_supported_answer를 거치지 않는 경로(answer_query의 미지원 답변)도 통과 지점이다."""
        assembler = QueryAnswerAssembler(FixedAnswerGenerator(""))

        answer, _ = assembler.renumber_used_evidence(
            GeneratedAnswer(content="근거가 없습니다. [doc_x:B0001]"), []
        )

        self.assertNotIn("doc_x", answer.content)
        self.assertNotIn("B0001", answer.content)

    def test_wikilinks_and_number_citations_survive(self) -> None:
        evidence_snippets = [
            EvidenceSnippet(rank=1, source_document_id="doc-a", source_block_ids=["B0001"], text="근거"),
        ]
        assembler = QueryAnswerAssembler(
            FixedAnswerGenerator("[[검색 인덱싱]] 문서를 보세요. [1]")
        )

        answer, _ = assembler.generate_supported_answer(query_context(evidence_snippets))

        self.assertIn("[[검색 인덱싱]]", answer.content)
        self.assertIn("[1]", answer.content)

    def test_removing_ref_does_not_leave_double_space(self) -> None:
        assembler = QueryAnswerAssembler(FixedAnswerGenerator(""))

        answer, _ = assembler.renumber_used_evidence(
            GeneratedAnswer(content="앞 문장. [doc_x:B0001] 뒤 문장."), []
        )

        self.assertNotIn("  ", answer.content)
        self.assertEqual("앞 문장. 뒤 문장.", answer.content)

    def test_answer_without_block_refs_keeps_surrounding_whitespace(self) -> None:
        """참조가 없는 답변은 손대지 않는다. 들여쓰기 코드블록이 앞에 오면 strip이 깨뜨린다."""
        assembler = QueryAnswerAssembler(FixedAnswerGenerator(""))

        answer, _ = assembler.renumber_used_evidence(
            GeneratedAnswer(content="    def foo():\n        pass\n"), []
        )

        self.assertEqual("    def foo():\n        pass\n", answer.content)

    def test_block_ref_at_line_start_keeps_paragraph_break(self) -> None:
        """줄 맨 앞 참조를 지울 때 앞의 빈 줄까지 먹으면 제목·문단이 붙어버린다."""
        assembler = QueryAnswerAssembler(FixedAnswerGenerator(""))

        answer, _ = assembler.renumber_used_evidence(
            GeneratedAnswer(content="## 소제목\n\n[doc_x:B0001] 설명입니다."), []
        )

        self.assertIn("## 소제목\n\n", answer.content)
        self.assertNotIn("B0001", answer.content)

    def test_returned_evidence_ranks_match_citations(self) -> None:
        evidence_snippets = [
            EvidenceSnippet(rank=4, source_document_id="doc-a", source_block_ids=["B0004"], text="첫 근거"),
            EvidenceSnippet(rank=7, source_document_id="doc-b", source_block_ids=["B0007"], text="둘째 근거"),
        ]
        assembler = QueryAnswerAssembler(
            FixedAnswerGenerator("첫 답변 [7], 보조 답변 [4, 7], 잘못된 인용 [99]")
        )

        answer, returned = assembler.generate_supported_answer(query_context(evidence_snippets))

        citation_ranks = {
            int(value)
            for marker in re.findall(r"\[((?:\d+)(?:\s*,\s*\d+)*)\]", answer.content)
            for value in marker.split(",")
        }
        self.assertEqual(citation_ranks, {1, 2})
        self.assertEqual([snippet.rank for snippet in returned], [1, 2])
        self.assertNotIn("[99]", answer.content)
        self.assertEqual([snippet.source_document_id for snippet in returned], ["doc-b", "doc-a"])

    def test_evidence_with_same_provenance_is_merged(self) -> None:
        evidence_snippets = [
            EvidenceSnippet(rank=1, source_document_id="doc-a", source_block_ids=["B0003"], text="첫 문장"),
            EvidenceSnippet(rank=2, source_document_id="doc-a", source_block_ids=["B0003"], text="둘째 문장"),
        ]
        assembler = QueryAnswerAssembler(FixedAnswerGenerator("첫 답변 [1][2], 보조 답변 [1, 2]"))

        answer, returned = assembler.generate_supported_answer(query_context(evidence_snippets))

        self.assertEqual(answer.content, "첫 답변 [1], 보조 답변 [1]")
        self.assertEqual(
            returned,
            [EvidenceSnippet(rank=1, source_document_id="doc-a", source_block_ids=["B0003"], text="첫 문장")],
        )


if __name__ == "__main__":
    unittest.main()
