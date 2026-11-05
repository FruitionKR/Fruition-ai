"""편집 세부 분류기 평가. 실제 모델을 호출해 evals/markdown_edit_detail.jsonl의 정답과 비교한다.

`--jev`면 운영과 같이 Jev로 분류한다. Jev가 실패한 요청은 운영처럼 LLM으로 넘어가며 그 횟수를 `jev_fallback_calls`로 센다.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from app.core.jev_client import JEV_EDIT_DETAIL_ENABLED_ENV
from app.core.llm_env import SUPPORTED_LLM_PROVIDERS
from app.modules.markdown_edit.domain.entities import MarkdownEditDetail, MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_edit_detail_classifier import (
    ChatCompletionsMarkdownEditDetailClassifier,
)
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import build_markdown_editor
from app.modules.markdown_edit.infrastructure.jev_markdown_edit_detail_classifier import JevMarkdownEditDetailClassifier

DEFAULT_DATASET = Path(__file__).resolve().parent / "evals" / "markdown_edit_detail.jsonl"
FIELDS = ("requested_formats", "addition_only", "structure_change", "target_language")


def load_cases(path: Path) -> list[dict[str, object]]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("편집 세부 분류 평가 case id는 중복될 수 없습니다.")
    return cases


def normalized(detail: dict[str, object]) -> dict[str, object]:
    return {**detail, "requested_formats": sorted(detail["requested_formats"])}  # type: ignore[arg-type]


class CountingClassifier:
    def __init__(self, classifier: ChatCompletionsMarkdownEditDetailClassifier) -> None:
        self._classifier = classifier
        self.calls = 0

    def classify(self, request: MarkdownEditRequest) -> MarkdownEditDetail:
        self.calls += 1
        return self._classifier.classify(request)


def evaluate_cases(
    classifier: ChatCompletionsMarkdownEditDetailClassifier | JevMarkdownEditDetailClassifier,
    cases: list[dict[str, object]],
) -> dict[str, object]:
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
    parser.add_argument("--jev", action="store_true", help="세부 분류를 Jev로 한다. TYPESAFE_API_KEY가 필요하다.")
    args = parser.parse_args()
    os.environ[JEV_EDIT_DETAIL_ENABLED_ENV] = "true" if args.jev else "false"
    editor = build_markdown_editor(provider=args.provider, model=args.model)
    classifier = editor._detail_classifier  # type: ignore[attr-defined]
    fallback = None
    if args.jev:
        # 키가 없으면 조용히 LLM으로 돌아 끔과 같은 값을 켬 결과로 잘못 보고하게 된다.
        if not isinstance(classifier, JevMarkdownEditDetailClassifier):
            parser.error("Jev를 쓸 수 없습니다. TYPESAFE_API_KEY를 설정하세요.")
        fallback = classifier._fallback = CountingClassifier(classifier._fallback)  # type: ignore[assignment]
    report = evaluate_cases(classifier, load_cases(args.dataset))
    if fallback is not None:
        report["summary"]["jev_fallback_calls"] = fallback.calls  # type: ignore[index]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = report["summary"]
    return int(summary["correct"] != summary["total"])  # type: ignore[index]


if __name__ == "__main__":
    raise SystemExit(main())
