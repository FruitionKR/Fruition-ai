"""편집 세부 분류기 평가. 실제 모델을 호출해 evals/markdown_edit_detail.jsonl의 정답과 비교한다."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from app.core.llm_env import SUPPORTED_LLM_PROVIDERS
from app.modules.markdown_edit.domain.entities import MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_edit_detail_classifier import (
    ChatCompletionsMarkdownEditDetailClassifier,
)
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import build_markdown_editor

DEFAULT_DATASET = Path(__file__).resolve().parent / "evals" / "markdown_edit_detail.jsonl"
FIELDS = ("requested_formats", "addition_only", "structure_change", "target_language")


def load_cases(path: Path) -> list[dict[str, object]]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("편집 세부 분류 평가 case id는 중복될 수 없습니다.")
    return cases


def normalized(detail: dict[str, object]) -> dict[str, object]:
    return {**detail, "requested_formats": sorted(detail["requested_formats"])}  # type: ignore[arg-type]


def evaluate_cases(classifier: ChatCompletionsMarkdownEditDetailClassifier, cases: list[dict[str, object]]) -> dict[str, object]:
    results = []
    for case in cases:
        actual = normalized(asdict(classifier.classify(MarkdownEditRequest(
            instruction=str(case["message"]),
            markdown="평가용 문서",
            target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1),
        ))))
        answers = [normalized(case["expected"]), *(normalized(item) for item in case.get("acceptable", []))]  # type: ignore[arg-type]
        results.append({
            "id": case["id"],
            "correct": actual in answers,
            "field_correct": {field: any(actual[field] == answer[field] for answer in answers) for field in FIELDS},
            "actual": actual,
            "expected": answers[0],
        })
    return {
        "summary": {
            "total": len(results),
            "correct": sum(result["correct"] for result in results),
            "field_correct": {field: sum(result["field_correct"][field] for result in results) for field in FIELDS},
        },
        "failures": [result for result in results if not result["correct"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="편집 세부 분류기 평가")
    parser.add_argument("--provider", choices=SUPPORTED_LLM_PROVIDERS, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    editor = build_markdown_editor(provider=args.provider, model=args.model)
    report = evaluate_cases(editor._detail_classifier, load_cases(args.dataset))  # type: ignore[attr-defined]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = report["summary"]
    return int(summary["correct"] != summary["total"])  # type: ignore[index]


if __name__ == "__main__":
    raise SystemExit(main())
