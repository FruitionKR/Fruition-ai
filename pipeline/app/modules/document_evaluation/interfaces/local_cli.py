import argparse
import time
from pathlib import Path

from app.modules.document_evaluation.application.models import (
    LocalDocumentEvaluationCommand,
)
from app.modules.document_evaluation.infrastructure.local_document_evaluator import (
    DEFAULT_ENDPOINT,
    evaluate,
    read_restoration_elapsed_seconds,
    record_evaluation_timing,
    write_artifacts,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown-file", type=Path, required=True)
    parser.add_argument("--pdf-file", type=Path, required=True)
    parser.add_argument("--output-file", type=Path, required=True)
    parser.add_argument("--output-markdown-file", type=Path)
    parser.add_argument("--output-report-file", type=Path)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--evaluator-model", default="qwen2.5:7b")
    parser.add_argument("--vision-model", default="qwen2.5vl:7b")
    parser.add_argument("--max-blocks", type=int, default=12)
    parser.add_argument("--max-chars", type=int, default=6000)
    parser.add_argument("--max-vision-attempts", type=int, choices=range(1, 3), default=2)
    parser.add_argument("--max-vision-requests", type=int, default=0, help="0이면 전체 요청을 처리")
    parser.add_argument("--max-chunks", type=int, default=0, help="0이면 모든 chunk를 평가")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    command = LocalDocumentEvaluationCommand(**vars(args))
    started_at = time.perf_counter()
    report = evaluate(command)
    record_evaluation_timing(
        report,
        evaluation_elapsed_seconds=time.perf_counter() - started_at,
        restoration_elapsed_seconds=read_restoration_elapsed_seconds(command.markdown_file),
    )
    write_artifacts(command, report)
    print(command.output_file)
    if command.output_markdown_file:
        print(command.output_markdown_file)
    if command.output_report_file:
        print(command.output_report_file)


if __name__ == "__main__":
    main()
