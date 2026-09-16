from __future__ import annotations

import unittest

from app.modules.document_restoration.domain.text_quality import (
    language_score,
    looks_glyph_encoded,
)


def encode_shifted_ascii(text: str) -> str:
    encoded = []
    for char in text:
        code = ord(char)
        encoded.append(chr(code - 29) if 29 <= code <= 122 else char)
    return "".join(encoded)


class TextQualityTest(unittest.TestCase):
    def test_shift_encoded_sentence_is_detected_without_token_dictionary(self) -> None:
        source = "The proposed method is evaluated with the original design."
        encoded = encode_shifted_ascii(source)

        self.assertTrue(looks_glyph_encoded(encoded))
        self.assertGreater(language_score(source), language_score(encoded))

    def test_normal_technical_sentence_is_not_detected(self) -> None:
        source = "Finite element analysis uses a three dimensional electromagnetic model."

        self.assertFalse(looks_glyph_encoded(source))

    def test_shift_encoded_caption_is_detected_without_common_words(self) -> None:
        source = "Finite element model mesh generation."
        encoded = encode_shifted_ascii(source)

        self.assertTrue(looks_glyph_encoded(encoded))

    def test_short_equation_token_is_not_treated_as_encoded_text(self) -> None:
        self.assertFalse(looks_glyph_encoded("SNR"))
