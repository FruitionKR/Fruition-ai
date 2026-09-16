from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass

from app.modules.markdown_edit.domain.entities import MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.domain.markdown_target_scope import build_markdown_target_scope
from app.modules.markdown_edit.infrastructure.markdown_source_range import (
    build_source_range_plan,
    source_range_payload,
    validate_markdown_target_boundary,
)


@dataclass(frozen=True)
class BenchmarkResult:
    document_lines: int
    document_chars: int
    request_payload_chars: int
    payload_ratio: float
    context_before_lines: int
    context_after_lines: int
    source_segment_count: int
    average_ms: float
    p95_ms: float


def _document(line_count: int) -> str:
    return "\n".join(f"운영 점검 문장 {line_number:05d}: 배포 전에 확인을 진행한다." for line_number in range(1, line_count + 1))


def run_benchmark(line_count: int, context_lines: int, repeat: int) -> BenchmarkResult:
    markdown = _document(line_count)
    target_line = line_count // 2
    target = MarkdownEditTarget(type="selection", start_line=target_line, end_line=target_line)
    durations: list[float] = []
    payload: dict[str, object] = {}
    scope = None
    plan = None

    for _ in range(repeat):
        started_at = time.perf_counter()
        validate_markdown_target_boundary(markdown, target)
        scope = build_markdown_target_scope(markdown, target, context_lines)
        plan = build_source_range_plan(
            MarkdownEditRequest(
                instruction="선택한 문장을 간결하게 다듬어줘.",
                markdown=scope.markdown,
                target=target,
                edit_goal="cleanup",
            )
        )
        if plan is None:
            raise RuntimeError("source range plan 생성에 실패했습니다.")
        payload = {
            "instruction": "선택한 문장을 간결하게 다듬어줘.",
            "edit_goal": "cleanup",
            "conversation_summary": None,
            "target": {"type": target.type, "start_line": target.start_line, "end_line": target.end_line},
            **source_range_payload(plan),
            "read_only_context": {
                "before": scope.context_before,
                "after": scope.context_after,
            },
        }
        durations.append((time.perf_counter() - started_at) * 1000)

    assert scope is not None and plan is not None
    serialized_payload = json.dumps(payload, ensure_ascii=False, indent=2)
    sorted_durations = sorted(durations)
    p95_index = min(len(sorted_durations) - 1, max(0, round(len(sorted_durations) * 0.95) - 1))
    return BenchmarkResult(
        document_lines=line_count,
        document_chars=len(markdown),
        request_payload_chars=len(serialized_payload),
        payload_ratio=round(len(serialized_payload) / len(markdown), 4),
        context_before_lines=len(scope.context_before.splitlines()),
        context_after_lines=len(scope.context_after.splitlines()),
        source_segment_count=len(plan.segments),
        average_ms=round(statistics.mean(durations), 3),
        p95_ms=round(sorted_durations[p95_index], 3),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Markdown 선택 편집 컨텍스트 크기 벤치마크")
    parser.add_argument("--lines", type=int, action="append", dest="line_counts")
    parser.add_argument("--context-lines", type=int, default=20)
    parser.add_argument("--repeat", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    line_counts = args.line_counts or [1000, 5000]
    if args.context_lines < 0 or args.repeat < 1 or any(line_count < 3 for line_count in line_counts):
        raise SystemExit("lines >= 3, context-lines >= 0, repeat >= 1 이어야 합니다.")
    results = [run_benchmark(line_count, args.context_lines, args.repeat) for line_count in line_counts]
    output = {
        "context_lines": args.context_lines,
        "repeat": args.repeat,
        "results": [asdict(result) for result in results],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
