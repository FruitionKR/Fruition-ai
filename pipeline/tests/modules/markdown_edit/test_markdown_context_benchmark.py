from markdown_context_benchmark import run_benchmark


def test_context_payload_stays_bounded_as_document_grows() -> None:
    small = run_benchmark(line_count=100, context_lines=20, repeat=1)
    large = run_benchmark(line_count=500, context_lines=20, repeat=1)

    assert small.context_before_lines == large.context_before_lines == 20
    assert small.context_after_lines == large.context_after_lines == 20
    assert abs(small.request_payload_chars - large.request_payload_chars) < 20
    assert large.payload_ratio < small.payload_ratio
