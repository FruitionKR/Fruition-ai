import unittest

from app.modules.query.application.evidence_text import (
    clean_sentence,
    split_paragraphs,
    split_sentences,
    split_structured_evidence_units,
    tokens,
)
from app.modules.query.infrastructure.rule_based_query_rewriter import RuleBasedQueryRewriter


class EvidenceTextTest(unittest.TestCase):
    def test_splits_bullets_with_block_refs_as_evidence_units(self) -> None:
        paragraph = "## Key Points\n- 첫 번째 근거입니다. [B0001]\n- 두 번째 근거입니다. [B0002]"

        units = split_structured_evidence_units(paragraph)

        self.assertEqual(
            units,
            [
                ("첫 번째 근거입니다. [B0001]", "key points", 1.35),
                ("두 번째 근거입니다. [B0002]", "key points", 1.35),
            ],
        )

    def test_keeps_ref_only_sentence_with_previous_sentence(self) -> None:
        self.assertEqual(
            split_sentences("근거 문장입니다. [B0001]"),
            ["근거 문장입니다. [B0001]"],
        )

    def test_ignores_frontmatter_and_heading_only_paragraphs(self) -> None:
        self.assertEqual(
            split_paragraphs("---\ntype: source\n---\n\n# 제목\n\n본문입니다. [B0001]"),
            ["본문입니다. [B0001]"],
        )

    def test_cleans_sentence_and_normalizes_korean_particles(self) -> None:
        self.assertEqual(clean_sentence("- Evidence 테스트입니다. [B0001]"), "테스트입니다. [B0001]")
        self.assertEqual(tokens("모터가 토크를 만든다"), ["모터", "토크", "만든다"])

    def test_normalizes_compound_particle_without_repeated_stripping(self) -> None:
        self.assertEqual(tokens("문서에서는 검색한다"), ["문서", "검색한다"])
        self.assertEqual(
            tokens("나에서는 가에서는 하나에서는 검색한다"),
            ["나에서는", "가에서는", "하나", "검색한다"],
        )
        self.assertEqual(tokens("English terms"), ["english", "terms"])
        self.assertEqual(
            RuleBasedQueryRewriter().rewrite("문서에서는 하나를 검색한다").keywords,
            ["문서", "하나", "검색한다"],
        )


if __name__ == "__main__":
    unittest.main()
