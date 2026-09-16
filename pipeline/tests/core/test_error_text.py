from app.core.error_text import truncate_error


def test_keeps_short_error_text() -> None:
    assert truncate_error(ValueError("오류"), limit=10) == "오류"


def test_truncates_error_with_ellipsis() -> None:
    assert truncate_error("abcdefghij", limit=7) == "abcd..."
