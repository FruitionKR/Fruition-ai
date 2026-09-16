import unittest

from app.modules.query.application.answer_context_formatter import AnswerContextFormatter
from app.modules.query.domain.entities import EvidenceSnippet, RetrievedPage, TraversalPath, WikiPage


def related_page(page_id: str, title: str, page_type: str = "source") -> RetrievedPage:
    return RetrievedPage(
        page=WikiPage(
            id=page_id,
            page_type=page_type,
            title=title,
            slug=title.lower().replace(" ", "-"),
            summary=f"{title} summary",
        ),
        score=0.9,
        role="seed_source",
    )


class AnswerContextFormatterTest(unittest.TestCase):
    def test_formats_resolved_question_evidence_and_paths(self) -> None:
        formatter = AnswerContextFormatter(max_paragraph_chars=30)

        context = formatter.format(
            question="resolved question",
            original_question="original question",
            related_pages=[related_page("source:a", "Source A")],
            traversal_paths=[
                TraversalPath(
                    path_id="path-1",
                    role="primary_answer_path",
                    nodes=["source:a"],
                    edges=[],
                    score=0.9,
                )
            ],
            evidence_snippets=[
                EvidenceSnippet(
                    rank=1,
                    source_document_id="doc-a",
                    source_block_ids=["B0001"],
                    text="This evidence text is intentionally longer than the formatter limit.",
                )
            ],
        )

        self.assertIn("# User Question\noriginal question", context)
        self.assertIn("# Resolved Retrieval Question\nresolved question", context)
        self.assertIn("# Evidence Snippets By Relevance", context)
        self.assertIn("This evidence text is inten...", context)
        self.assertIn("- titles: Source A", context)
        self.assertIn("answer that supported part first", context)
        self.assertIn("provided internal documents do not support", context)

    def test_adds_web_fallback_policy_for_web_answer_mode(self) -> None:
        context = AnswerContextFormatter().format(
            question="question",
            related_pages=[],
            traversal_paths=[],
            evidence_snippets=[],
            answer_mode="web_fallback",
        )

        self.assertIn("# Web Fallback Answer Policy", context)
        self.assertIn("Do not answer as an unsupported/refusal response", context)


if __name__ == "__main__":
    unittest.main()
