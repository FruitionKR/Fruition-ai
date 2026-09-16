import unittest
from dataclasses import dataclass

from app.modules.wiki_generation.application.section_polish_mapping import map_polish_output


@dataclass
class FakeBlock:
    block_id: str


class SectionPolishMappingTest(unittest.TestCase):
    def test_maps_refs_and_cleans_inline_block_markers(self) -> None:
        warnings: list[str] = []

        mapped = map_polish_output(
            {
                "section": "definition",
                "text": "  Back EMF 설명 [B0001]\n입니다.  ",
                "anchor_block_ids": ["B0001", "B9999"],
                "items": [
                    {"text": "핵심 포인트 (B0002)", "anchor_block_ids": ["B0002"]},
                    {"text": "", "anchor_block_ids": ["B9999"]},
                ],
                "related_concept_hints": ["torque-ripple"],
                "confidence": 0.8,
            },
            [FakeBlock("B0001"), FakeBlock("B0002")],
            warnings,
            "concept:back-emf",
        )

        self.assertEqual(mapped["section"], "definition")
        self.assertEqual(mapped["text"], "Back EMF 설명 입니다.")
        self.assertEqual(mapped["anchor_reference_ids"], ["B0001"])
        self.assertEqual(
            mapped["items"],
            [
                {"text": "핵심 포인트", "anchor_reference_ids": ["B0002"]},
                {"text": "", "anchor_reference_ids": []},
            ],
        )
        self.assertEqual(mapped["related_concept_hints"], ["torque-ripple"])
        self.assertEqual(mapped["confidence"], 0.8)
        self.assertEqual(
            warnings,
            [
                "concept:back-emf: unknown polish anchor_block_id B9999",
                "concept:back-emf: unknown polish anchor_block_id B9999",
            ],
        )


if __name__ == "__main__":
    unittest.main()
