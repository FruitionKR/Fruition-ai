import unittest

from app.modules.wiki_generation.infrastructure.concept_page_sections import (
    collect_source_key_points,
    concept_evidence,
    concept_key_points_from_source,
    concept_related_lines,
    item_refs,
    shared_key_point_pairs,
)


class ConceptPageSectionsTest(unittest.TestCase):
    def test_collects_key_points_and_prefers_anchor_reference_ids(self) -> None:
        normalized = {
            "semantic_notes": [
                {
                    "key_points": [
                        {"text": "Back EMF는 핵심 지표다.", "anchor_reference_ids": ["B0001"], "anchor_block_ids": ["B9999"]},
                        {"text": "Back EMF는 핵심 지표다.", "anchor_reference_ids": ["B0002"]},
                        {"text": "Torque Ripple도 함께 본다.", "anchor_block_ids": ["B0003"]},
                    ]
                }
            ]
        }

        self.assertEqual(item_refs(normalized["semantic_notes"][0]["key_points"][0]), ["B0001"])
        self.assertEqual(
            collect_source_key_points(normalized),
            [
                {"text": "Back EMF는 핵심 지표다.", "anchor_reference_ids": ["B0001"]},
                {"text": "Torque Ripple도 함께 본다.", "anchor_reference_ids": ["B0003"]},
            ],
        )

    def test_uses_direct_evidence_before_ref_fallback(self) -> None:
        concept = {"slug": "back-emf", "display_reference_ids": ["B0001"]}
        fallback = {"claim": "ref fallback", "anchor_reference_ids": ["B0001"], "related_concept_slugs": []}
        direct = {"claim": "direct", "anchor_reference_ids": ["B0002"], "related_concept_slugs": ["back-emf"]}

        self.assertEqual(concept_evidence(concept, [fallback, direct]), [direct])
        self.assertEqual(concept_evidence(concept, [fallback]), [fallback])

    def test_builds_key_points_and_related_lines_from_shared_refs(self) -> None:
        normalized = {
            "evidence_units": [
                {"anchor_reference_ids": ["B0001"], "related_concept_slugs": ["back-emf", "torque-ripple"]},
            ],
            "concept_resolutions": [
                {"canonical_slug": "back-emf", "link_targets": ["manufacturing-tolerance"]},
            ],
            "hint_resolutions": [],
        }
        ledger_by_slug = {
            "back-emf": {"slug": "back-emf", "title": "Back EMF", "display_reference_ids": ["B0001"]},
            "torque-ripple": {"slug": "torque-ripple", "title": "Torque Ripple", "display_reference_ids": ["B0001"]},
            "manufacturing-tolerance": {"slug": "manufacturing-tolerance", "title": "Manufacturing Tolerance"},
        }
        source_key_points = [{"text": "Back EMF와 Torque Ripple을 함께 검토한다.", "anchor_reference_ids": ["B0001"]}]

        self.assertEqual(
            concept_key_points_from_source(
                ledger_by_slug["back-emf"],
                normalized["evidence_units"],
                source_key_points,
                "doc_a",
            ),
            ["- Back EMF와 Torque Ripple을 함께 검토한다. [doc_a:B0001]"],
        )
        self.assertEqual(
            shared_key_point_pairs(normalized, ledger_by_slug, source_key_points),
            [("back-emf", "torque-ripple", "shared_source_key_point")],
        )
        self.assertEqual(
            concept_related_lines("back-emf", normalized, ledger_by_slug, source_key_points),
            ["- [[torque-ripple|Torque Ripple]]", "- [[manufacturing-tolerance|Manufacturing Tolerance]]"],
        )


if __name__ == "__main__":
    unittest.main()
