import unittest

from app.modules.wiki_ingestion.infrastructure.promotion_concept_page import (
    build_promotion_concept_page,
    cite_lint_refs,
    normalize_lint_refs,
    promotion_representative,
)


class PromotionConceptPageTest(unittest.TestCase):
    def test_normalizes_refs_from_global_or_block_id(self) -> None:
        allowed_refs = {"doc_a:B0001", "doc_b:B0002"}
        source_ref_by_block = {"B0001": "doc_a:B0001", "B0002": "doc_b:B0002"}

        self.assertEqual(
            normalize_lint_refs(["B0001", "doc_b:B0002", "missing", "B0001"], allowed_refs, source_ref_by_block),
            ["doc_a:B0001", "doc_b:B0002"],
        )
        self.assertEqual(cite_lint_refs(["doc_a:B0001", ""]), " [doc_a:B0001]")

    def test_builds_page_from_draft_and_cluster_fallbacks(self) -> None:
        cluster = {
            "id": "back-emf-cluster",
            "representative": "Back EMF",
            "claims": [
                {
                    "id": "claim_001",
                    "claim": "Back EMF는 제조 공차의 영향을 받는다.",
                    "refs": ["doc_a:B0001"],
                }
            ],
            "relations": [
                {"target": "concept:tolerance-analysis", "relation": "supports_or_enables"},
            ],
        }
        draft = {
            "slug": "Back EMF",
            "title": "Adversarial Draft Title",
            "definition": {"text": "회전 전동기에서 유기되는 역기전력이다.", "anchor_block_ids": ["B0001"]},
            "key_points": [{"text": "공차 분석의 입력으로 쓰인다.", "anchor_block_ids": ["doc_a:B0001"]}],
            "evidence": [],
            "related_concept_hints": ["Manufacturing Tolerance"],
            "why_it_matters": "성능 평가에 필요하다.",
            "confidence": 0.8,
        }

        page = build_promotion_concept_page(
            cluster,
            draft,
            allowed_refs={"doc_a:B0001"},
            source_ref_by_block={"B0001": "doc_a:B0001"},
        )

        self.assertEqual(promotion_representative(cluster), "Back EMF")
        self.assertEqual(page["slug"], "back-emf")
        self.assertEqual(page["title"], "Back EMF")
        self.assertNotIn("Adversarial Draft Title", page["markdown"])
        self.assertIn("sources: doc_a", page["markdown"])
        self.assertIn("회전 전동기에서 유기되는 역기전력이다. [doc_a:B0001]", page["markdown"])
        self.assertIn("- 공차 분석의 입력으로 쓰인다. [doc_a:B0001]", page["markdown"])
        self.assertIn("- claim_001: Back EMF는 제조 공차의 영향을 받는다. [doc_a:B0001]", page["markdown"])
        self.assertIn("- [[manufacturing-tolerance|Manufacturing Tolerance]]", page["markdown"])
        self.assertIn("- [[tolerance-analysis|tolerance-analysis]]", page["markdown"])

    def test_uses_id_when_representative_is_absent(self) -> None:
        page = build_promotion_concept_page(
            {
                "id": "orchid-lease",
                "claims": [{"id": "claim_001", "claim": "계약", "refs": []}],
            },
            {"title": "Adversarial Draft Title"},
            allowed_refs=set(),
            source_ref_by_block={},
        )

        self.assertEqual(page["title"], "orchid-lease")

    def test_preserves_global_source_refs_for_query_evidence(self) -> None:
        page = build_promotion_concept_page(
            {
                "id": "orchid-lease",
                "claims": [
                    {
                        "id": "claim_001",
                        "claim": "계약 조건",
                        "refs": ["document:B0001"],
                    }
                ],
            },
            {
                "definition": {
                    "text": "계약 정의",
                    "anchor_block_ids": ["document:B0001"],
                }
            },
            allowed_refs={"document:B0001"},
            source_ref_by_block={"B0001": "document:B0001"},
        )

        self.assertIn("계약 정의 [document:B0001]", page["markdown"])
        self.assertIn("claim_001: 계약 조건 [document:B0001]", page["markdown"])


if __name__ == "__main__":
    unittest.main()
