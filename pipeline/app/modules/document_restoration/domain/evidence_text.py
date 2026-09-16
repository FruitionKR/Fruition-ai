from __future__ import annotations


MAX_EVIDENCE_CHARS = 2500


def clip_evidence(text: str, max_chars: int = MAX_EVIDENCE_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return f"{text[:half]}\n...[evidence clipped]...\n{text[-half:]}"
