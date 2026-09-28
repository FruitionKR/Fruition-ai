import unittest

from app.modules.query.domain.scoring import evidence_embedding_weight


class EvidenceEmbeddingWeightTest(unittest.TestCase):
    def test_korean_question_uses_ten_percent_keyword_weight(self) -> None:
        self.assertAlmostEqual(evidence_embedding_weight("Transformer 학습률은 어떻게 바뀌었어?"), 0.9)

    def test_english_question_uses_forty_percent_keyword_weight(self) -> None:
        self.assertAlmostEqual(evidence_embedding_weight("How did the learning rate change?"), 0.6)

    def test_compatibility_jamo_counts_as_korean(self) -> None:
        self.assertAlmostEqual(evidence_embedding_weight("fruition ㅋㅋ"), 0.9)


if __name__ == "__main__":
    unittest.main()
