"""첨부와 함께 "이 문서"를 물었을 때의 답변 평가. 실제 모델을 호출해 evals/open_document_answer.jsonl과 비교한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.modules.agent.domain.reference_documents import SelectedReferenceDocuments, reference_evidence_snippets
from app.modules.query.application.answer_context_formatter import AnswerContextFormatter
from app.modules.query.domain.entities import GraphContext, QueryContext
from app.modules.query.infrastructure.query_chat_answer_generator import build_query_chat_answer_generator
from app.modules.skill.domain.entities import SkillAuthoringReference

DEFAULT_DATASET = Path(__file__).resolve().parent / "evals" / "open_document_answer.jsonl"


def load_cases(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate_case(generator: object, case: dict[str, object]) -> dict[str, object]:
    selected = SelectedReferenceDocuments(
        documents=(
            SkillAuthoringReference(id="open-doc", name="", markdown=str(case["open_document"])),
            SkillAuthoringReference(id="attachment", name=str(case["attachment_title"]), markdown=str(case["attachment"])),
        ),
        open_document_id="open-doc",
    )
    snippets = list(reference_evidence_snippets(selected))
    question = str(case["question"])
    answer = generator.generate_answer(  # type: ignore[attr-defined]
        QueryContext(
            question=question,
            graph_context=GraphContext(nodes=[], edges=[]),
            traversal_paths=[],
            related_pages=[],
            evidence_snippets=snippets,
            answer_context=AnswerContextFormatter().format(question, [], [], snippets),
        )
    ).content
    missing = [word for word in case["must_contain"] if word not in answer]  # type: ignore[union-attr]
    forbidden = [word for word in case["must_not_contain"] if word in answer]  # type: ignore[union-attr]
    return {"id": case["id"], "passed": not missing and not forbidden, "missing": missing, "forbidden": forbidden, "answer": answer}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="gpt-6-luna")
    args = parser.parse_args()
    generator = build_query_chat_answer_generator(provider=args.provider, model=args.model)
    results = [evaluate_case(generator, case) for case in load_cases(args.dataset)]
    print(json.dumps({"passed": sum(result["passed"] for result in results), "total": len(results), "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
