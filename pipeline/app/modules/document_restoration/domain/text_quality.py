from __future__ import annotations

import re


COMMON_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
}


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def shifted_ascii_candidate(text: str) -> str:
    decoded = []
    for char in text:
        code = ord(char)
        decoded.append(chr(code + 29) if 0 <= code <= 93 else char)
    return normalize_text("".join(decoded))


def language_score(text: str) -> float:
    normalized = normalize_text(text)
    if not normalized:
        return -100.0
    words = re.findall(r"[A-Za-z]+", normalized)
    alpha_count = sum(char.isalpha() for char in normalized)
    vowel_count = sum(char.lower() in "aeiou" for char in normalized if char.isalpha())
    common_count = sum(word.lower() in COMMON_WORDS for word in words)
    symbol_runs = len(re.findall(r"[^\w\s.,:;!?%+\-/()]{2,}", normalized))
    mixed_tokens = len(re.findall(r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]+\b", normalized))
    consonant_upper = sum(
        1
        for word in words
        if len(word) >= 3 and word.isupper() and not re.search(r"[AEIOU]", word)
    )
    return (
        alpha_count / max(1, len(normalized))
        + vowel_count / max(1, alpha_count) * 0.3
        + common_count / max(1, len(words)) * 0.5
        - symbol_runs * 0.3
        - mixed_tokens / max(1, len(words)) * 0.5
        - consonant_upper / max(1, len(words)) * 0.4
    )


def looks_glyph_encoded(text: str) -> bool:
    normalized = normalize_text(text)
    if len(normalized) < 6:
        return False
    words = re.findall(r"[A-Za-z]+", normalized)
    consonant_upper = [
        word
        for word in words
        if len(word) >= 3 and word.isupper() and not re.search(r"[AEIOU]", word)
    ]
    symbol_runs = len(re.findall(r"[,$%&'*/;<=>@\[\\\]^_`{|}~]{3,}", normalized))
    compact = re.sub(r"\s+", "", normalized)
    noisy_count = sum(char.isdigit() or char in "$%&*/;<=>@[\\]^_`{|}~" for char in compact)
    decoded = shifted_ascii_candidate(normalized)
    decoded_common = sum(word.lower() in COMMON_WORDS for word in re.findall(r"[A-Za-z]+", decoded))
    score_improvement = language_score(decoded) - language_score(normalized)
    uppercase_word_ratio = sum(word.isupper() for word in words) / max(1, len(words))
    shift_improves = (
        decoded_common >= 2 and score_improvement >= 0.2
    ) or (len(words) >= 3 and uppercase_word_ratio >= 0.8 and score_improvement >= 0.2)
    uppercase_consonant_ratio = len(consonant_upper) / max(1, len(words))
    return (
        symbol_runs > 0
        or shift_improves
        or (len(words) >= 4 and uppercase_consonant_ratio >= 0.5)
        or (noisy_count / max(1, len(compact)) > 0.35 and not re.search(r"[aeiou]", compact, flags=re.IGNORECASE))
    )
