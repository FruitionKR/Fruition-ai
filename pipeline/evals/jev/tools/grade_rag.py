"""RAG 결과를 Codex로 채점한다. 켬/끔을 가리고 순서를 섞어 20건씩 `codex exec`(읽기 전용)로 보낸다.

- 근거 충분성(evidence): 고른 근거 조각만으로 기대 답을 만들 수 있나. 근거 선택기 평가의 본 지표다.
  같은 질문에 같은 근거 묶음이 나온 실행은 한 번만 채점한다. 답이 없는 질문은 채점하지 않는다.
- 답변(answer): 기대 답과 맞는지(correctness), 답변이 받은 근거·페이지 요약에 없는 구체적 사실을 단정했는지(hallucination).
  다른 근거로 답했어도 받은 근거에 있는 내용이면 환각이 아니다.

사용: python grade_rag.py <rows.jsonl> [--first-batch-only]
결과: rows.jsonl 옆 evidence_grades.jsonl, answer_grades.jsonl, grade_map.json(블라인드 id → 원래 행)
"""
import argparse
import json
import random
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import EVAL_DIR, verify_gold  # noqa: E402

BATCH = 20
SNIPPET_CHARS = 900  # AnswerContextFormatter가 근거 조각을 자르는 길이
EVIDENCE_PROMPT = """You are grading evidence retrieved by a Korean/English workspace Q&A system. Grade each item independently.
Use only the item's question, expected_answer, acceptable_answers and evidence list. Do not use outside knowledge.
For each item return:
- support: "sufficient" if the evidence alone contains every fact needed to state the expected answer or one of
  acceptable_answers (paraphrase or a rewritten summary counts), "partial" if it contains some but not all of those facts, "none" if it contains none.
- used: the 1-based numbers of the evidence items that carry those facts (empty if none).
- note: one short Korean sentence explaining the grade.
Return JSON only, matching the schema. Items:
"""
ANSWER_PROMPT = """You are grading answers from a Korean/English workspace Q&A system. Grade each item independently.
Each item has the question, the expected answer, an answerable flag, the exact context the answering model received
(received_context: related page summaries and evidence snippets) and the answer. Do not use outside knowledge.
For each item return:
- correctness: "correct" if the answer states the expected answer or any of acceptable_answers (paraphrase is fine).
  "partial" only if a fact the question asks for is missing or a stated fact is wrong; do not lower the grade for
  omitting details the question did not ask for. "wrong" otherwise. For answerable=false items, "correct" means the answer says the
  information is not available and does not invent one; any made-up concrete answer is "wrong".
- hallucination: true only if the answer asserts a concrete fact (number, date, name, condition, rule) that is not
  supported by the received context. Facts taken from received context are not hallucinations even when they are not
  part of the expected answer. Saying information is unavailable is not a hallucination.
- note: one short Korean sentence explaining the grade, naming the unsupported fact if hallucination is true.
Return JSON only, matching the schema. Items:
"""


def schema(fields: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["grades"],
        "properties": {"grades": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["item_id", *fields], "properties": {"item_id": {"type": "string"}, **fields},
        }}},
    }


EVIDENCE_SCHEMA = schema({
    "support": {"type": "string", "enum": ["sufficient", "partial", "none"]},
    "used": {"type": "array", "items": {"type": "integer"}},
    "note": {"type": "string"},
})
ANSWER_SCHEMA = schema({
    "correctness": {"type": "string", "enum": ["correct", "partial", "wrong"]},
    "hallucination": {"type": "boolean"},
    "note": {"type": "string"},
})


def grade(items: list[dict], prompt: str, item_schema: dict, out_path: Path, first_batch_only: bool) -> None:
    done = {json.loads(line)["item_id"] for line in out_path.read_text().splitlines()} if out_path.exists() else set()
    pending = [item for item in items if item["item_id"] not in done]
    with tempfile.TemporaryDirectory() as tmp:
        schema_path = Path(tmp) / "schema.json"
        schema_path.write_text(json.dumps(item_schema))
        for start in range(0, len(pending), BATCH):
            batch = pending[start:start + BATCH]
            last = Path(tmp) / f"out_{start}.json"
            subprocess.run(
                ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--output-schema", str(schema_path),
                 "-o", str(last), prompt + json.dumps(batch, ensure_ascii=False, indent=1)],
                # stdin이 열려 있으면 codex가 추가 입력을 기다리며 멈춘다.
                check=True, cwd=tmp, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900,
            )
            grades = json.loads(last.read_text())["grades"]
            assert {g["item_id"] for g in grades} == {i["item_id"] for i in batch}, "채점 결과가 항목과 맞지 않습니다"
            with out_path.open("a") as f:
                f.writelines(json.dumps(g, ensure_ascii=False) + "\n" for g in grades)
            print(out_path.name, "graded", start + len(batch), "/", len(pending), flush=True)
            if first_batch_only:
                break


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("rows")
    parser.add_argument("--first-batch-only", action="store_true")
    args = parser.parse_args()
    verify_gold()
    cases = {c["id"]: c for c in json.loads((EVAL_DIR / "rag.json").read_text())}
    rows_path = Path(args.rows)
    rows = [json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
    rng = random.Random(20261030)
    new_id = lambda: uuid.UUID(int=rng.getrandbits(128)).hex[:12]  # noqa: E731

    mapping: dict[str, dict] = {"evidence": {}, "answer": {}}
    evidence_items: dict[tuple, dict] = {}
    answer_items = []
    for row in rows:
        case = cases[row["id"]]
        key = {"id": row["id"], "variant": row["variant"], "run": row["run"]}
        texts = [snippet["text"] for snippet in row["evidence"]]
        if case["answerable"] and texts:
            group = (row["id"], tuple(texts))
            if group not in evidence_items:
                evidence_items[group] = {
                    "item_id": new_id(), "question": case["question"], "expected_answer": case["expected_answer"],
                    "acceptable_answers": case.get("acceptable_answers", []),
                    "evidence": [f"[{i}] {text}" for i, text in enumerate(texts, start=1)],
                }
            mapping["evidence"].setdefault(evidence_items[group]["item_id"], []).append(key)
        item_id = new_id()
        mapping["answer"][item_id] = key
        answer_items.append({
            "item_id": item_id, "question": case["question"], "answerable": case["answerable"],
            "expected_answer": case["expected_answer"], "acceptable_answers": case.get("acceptable_answers", []),
            # 답변 모델이 실제로 받은 프롬프트 문맥(근거 조각·페이지 요약). 없으면 저장된 근거로 대신한다.
            "received_context": row.get("answer_context") or "\n".join(
                [*(f"[{i}] {text[:SNIPPET_CHARS]}" for i, text in enumerate(texts, start=1)),
                 *(f"{page['title']}: {page['summary']}" for page in row.get("pages", []))]
            ),
            "answer": row["answer"] or "(오류로 답변 없음)",
        })
    evidence_list = list(evidence_items.values())
    rng.shuffle(evidence_list)
    rng.shuffle(answer_items)
    (rows_path.parent / "grade_map.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=1))
    print("근거 채점", len(evidence_list), "묶음 (행", sum(map(len, mapping["evidence"].values())), ") / 답변 채점", len(answer_items), "건")
    grade(evidence_list, EVIDENCE_PROMPT, EVIDENCE_SCHEMA, rows_path.parent / "evidence_grades.jsonl", args.first_batch_only)
    grade(answer_items, ANSWER_PROMPT, ANSWER_SCHEMA, rows_path.parent / "answer_grades.jsonl", args.first_batch_only)


if __name__ == "__main__":
    main()
