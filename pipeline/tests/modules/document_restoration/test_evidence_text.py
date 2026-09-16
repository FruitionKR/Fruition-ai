from app.modules.document_restoration.domain.evidence_text import clip_evidence


def test_keeps_short_evidence() -> None:
    assert clip_evidence("근거", max_chars=10) == "근거"


def test_clips_both_ends_of_long_evidence() -> None:
    assert clip_evidence("abcdefghij", max_chars=6) == "abc\n...[evidence clipped]...\nhij"
