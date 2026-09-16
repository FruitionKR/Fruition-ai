import argparse
import json
from pathlib import Path

from app.modules.document_evaluation.application.prepare_document_evaluation import (
    prepare_document_evaluation,
)
from app.modules.document_evaluation.infrastructure.assembled_markdown_parser import (
    parse_assembled_markdown,
)
from app.modules.document_evaluation.infrastructure.chat_completions_document_evaluator import (
    build_optional_document_evaluator,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown-file", type=Path, required=True)
    parser.add_argument("--pdf-file", type=Path, required=True)
    parser.add_argument("--job-file", type=Path, required=True)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--max-blocks", type=int, default=12)
    parser.add_argument("--max-chars", type=int, default=6000)
    args = parser.parse_args()

    markdown = args.markdown_file.read_text(encoding="utf-8")
    job, result = prepare_document_evaluation(
        markdown=markdown,
        pdf_reference=str(args.pdf_file),
        blocks=parse_assembled_markdown(markdown),
        evaluator=build_optional_document_evaluator(),
        max_blocks=args.max_blocks,
        max_chars=args.max_chars,
    )
    args.job_file.parent.mkdir(parents=True, exist_ok=True)
    args.job_file.write_text(
        json.dumps(job.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.job_file)

    if result is None:
        print("pending_external_evaluator")
        return
    result_file = args.result_file or args.job_file.with_suffix(".result.json")
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(result_file)


if __name__ == "__main__":
    main()
