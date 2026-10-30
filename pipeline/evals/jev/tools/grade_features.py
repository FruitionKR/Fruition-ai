"""문서 편집·Skill 시험 실행 결과를 사례별 판정 기준으로 Codex가 채점한다. 같은 결과 문서는 한 번만 채점한다.

사용: python grade_features.py edit ../results/<날짜>/edit/rows.jsonl
      python grade_features.py skill ../results/<날짜>/skill/rows.jsonl
결과: rows.jsonl 옆 judge_grades.jsonl(결과별 판정)과 judged_rows.jsonl(행마다 code_ok·judge_ok·ok)
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import EVAL_DIR, verify_gold, write_jsonl  # noqa: E402
from grade_rag import grade, schema  # noqa: E402

PROMPT = """You are grading the output of a Korean/English document editing feature. Grade each item independently.
Each item has the user's request, the original Markdown (with line numbers), the target line range, the resulting
Markdown, the success criteria and notes on acceptable variations. Judge only against the criteria and notes; do not
penalize wording, formatting or length choices the criteria do not constrain. Do not use outside knowledge except to
check that no fact absent from the original was added.
For each item return:
- passed: true only if every criterion holds.
- failed_criteria: the 1-based numbers of the criteria that do not hold (empty if passed).
- note: one short Korean sentence explaining the grade, naming the failed point if any.
Return JSON only, matching the schema. Items:
"""
SCHEMA = schema({
    "passed": {"type": "boolean"},
    "failed_criteria": {"type": "array", "items": {"type": "integer"}},
    "note": {"type": "string"},
})


def numbered(markdown: str) -> str:
    return "\n".join(f"{i:>3} | {line}" for i, line in enumerate(markdown.split("\n"), start=1))


def main() -> None:
    kind, rows_path = sys.argv[1], Path(sys.argv[2])
    verify_gold()
    cases = {c["id"]: c for c in json.loads((EVAL_DIR / f"{kind}.json").read_text())}
    rows = [json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
    items: dict[str, dict] = {}
    for row in rows:
        case = cases[row["id"]]
        if row.get("result_markdown") is None or (kind == "skill" and case["kind"] != "test_run"):
            continue
        item_id = hashlib.sha256(f"{row['id']}\n{row['result_markdown']}".encode()).hexdigest()[:12]
        row["judge_item"] = item_id
        if item_id in items:
            continue
        request = case["instruction"] if kind == "edit" else f"Skill '{case['skill']['name']}'을 문서에 적용:\n{case['skill']['instructions_markdown']}"
        items[item_id] = {
            "item_id": item_id, "request": request, "target": case.get("target"), "original_markdown": numbered(case["markdown"]),
            "result_markdown": row["result_markdown"], "criteria": [f"{i}. {c}" for i, c in enumerate(case["criteria"], start=1)],
            "acceptable_notes": case.get("acceptable_notes", ""),
        }
    out = rows_path.parent / "judge_grades.jsonl"
    grade(list(items.values()), PROMPT, SCHEMA, out, first_batch_only=False)
    grades = {g["item_id"]: g for g in map(json.loads, out.read_text().splitlines())}
    for row in rows:
        if kind == "skill" and cases[row["id"]]["kind"] != "test_run":
            row["judge_ok"] = None
            continue
        judged = grades.get(row.get("judge_item"))
        row["judge_ok"] = bool(judged and judged["passed"])
        row["judge_note"] = judged["note"] if judged else "결과 없음"
        code_ok = row["code_ok"] if kind == "edit" else row["ok"]
        row["code_ok"] = code_ok
        row["ok"] = bool(code_ok and row["judge_ok"] and not row.get("error"))
    write_jsonl(rows_path.parent / "judged_rows.jsonl", rows)
    mine = [r for r in rows if r.get("judge_ok") is not None]
    print("성공", sum(r["ok"] for r in mine), "/", len(mine), "| 코드 검사", sum(bool(r["code_ok"]) for r in mine), "| 판정", sum(r["judge_ok"] for r in mine))


if __name__ == "__main__":
    main()
