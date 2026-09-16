from __future__ import annotations

import unittest

from app.modules.document_restoration.infrastructure.build_docling_primary_manifest import (
    associate_picture_captions,
)


def item(ref: str, label: str, page: int, bbox: tuple[float, float, float, float], text: str = "") -> dict:
    return {
        "self_ref": ref,
        "label": label,
        "text": text,
        "prov": [
            {
                "page_no": page,
                "bbox": {
                    "l": bbox[0],
                    "t": bbox[1],
                    "r": bbox[2],
                    "b": bbox[3],
                    "coord_origin": "TOPLEFT",
                },
            }
        ],
    }


class FigureCaptionMatchingTest(unittest.TestCase):
    def test_associates_nearby_figure_caption_without_explicit_reference(self) -> None:
        picture = item("#/pictures/0", "picture", 1, (100, 100, 300, 220))
        caption = item("#/texts/0", "caption", 1, (90, 230, 310, 250), "FIGURE 1 Motor geometry.")

        result = associate_picture_captions([picture], {caption["self_ref"]: caption}, {"1": {"size": {"height": 800}}})

        self.assertEqual(result[picture["self_ref"]], caption)

    def test_does_not_associate_distant_caption(self) -> None:
        picture = item("#/pictures/0", "picture", 1, (100, 100, 300, 220))
        caption = item("#/texts/0", "caption", 1, (90, 400, 310, 420), "FIGURE 1 Motor geometry.")

        result = associate_picture_captions([picture], {caption["self_ref"]: caption}, {"1": {"size": {"height": 800}}})

        self.assertEqual(result, {})

    def test_ignores_table_caption(self) -> None:
        picture = item("#/pictures/0", "picture", 1, (100, 100, 300, 220))
        caption = item("#/texts/0", "caption", 1, (90, 230, 310, 250), "TABLE 1 Motor parameters.")

        result = associate_picture_captions([picture], {caption["self_ref"]: caption}, {"1": {"size": {"height": 800}}})

        self.assertEqual(result, {})

    def test_keeps_explicit_caption_reference_when_text_is_encoded(self) -> None:
        picture = item("#/pictures/0", "picture", 1, (100, 100, 300, 220))
        picture["captions"] = [{"$ref": "#/texts/0"}]
        caption = item("#/texts/0", "caption", 1, (90, 230, 310, 250), ")LJXUH PRWRU JHRPHWU\x7f")

        result = associate_picture_captions([picture], {caption["self_ref"]: caption}, {"1": {"size": {"height": 800}}})

        self.assertEqual(result[picture["self_ref"]], caption)
