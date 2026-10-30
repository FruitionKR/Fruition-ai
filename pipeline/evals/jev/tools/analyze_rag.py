"""RAG 채점 결과를 워크스페이스(편입 방식) × Jev 켬/끔으로 집계한다.

사용: python analyze_rag.py <결과폴더>... → stdout 요약, <첫 폴더>/../rag_summary.json
"""
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import EVAL_DIR  # noqa: E402


def load(folder: Path) -> list[dict]:
    rows = {(r["id"], r["variant"], r["run"]): r for r in map(json.loads, (folder / "rows.jsonl").open())}
    mapping = json.loads((folder / "grade_map.json").read_text())
    for line in (folder / "evidence_grades.jsonl").open():
        grade = json.loads(line)
        for key in mapping["evidence"][grade["item_id"]]:
            rows[(key["id"], key["variant"], key["run"])]["evidence_grade"] = grade
    for line in (folder / "answer_grades.jsonl").open():
        grade = json.loads(line)
        key = mapping["answer"][grade["item_id"]]
        rows[(key["id"], key["variant"], key["run"])]["answer_grade"] = grade
    return list(rows.values())


def summarize(rows: list[dict]) -> dict:
    answerable = [r for r in rows if r["answerable"]]
    support = collections.Counter(r.get("evidence_grade", {}).get("support", "none") for r in answerable)
    correctness = collections.Counter(r["answer_grade"]["correctness"] for r in rows)
    n, na = len(rows), len(answerable)
    # 실패 원인 나누기: 근거가 충분했는데 틀리면 답변 생성, 근거가 없거나 일부인데 틀리면 근거 선택.
    wrong = [r for r in answerable if r["answer_grade"]["correctness"] != "correct"]
    return {
        "runs": n,
        "evidence_sufficient": support["sufficient"] / na,
        "evidence_partial": support["partial"] / na,
        "evidence_none": support["none"] / na,
        "correct": correctness["correct"] / n,
        "partial": correctness["partial"] / n,
        "wrong": correctness["wrong"] / n,
        "hallucination": sum(r["answer_grade"]["hallucination"] for r in rows) / n,
        "unanswerable_correct": (
            sum(r["answer_grade"]["correctness"] == "correct" for r in rows if not r["answerable"])
            / max(1, sum(not r["answerable"] for r in rows))
        ),
        "fail_generation": sum(r.get("evidence_grade", {}).get("support") == "sufficient" for r in wrong) / na,
        "fail_retrieval": sum(r.get("evidence_grade", {}).get("support") != "sufficient" for r in wrong) / na,
        "code_recall": sum(r["recall"] for r in answerable if r["recall"] is not None) / na,
        "evidence_count": sum(r["evidence_count"] for r in rows) / n,
        "seconds": sum(r["seconds"] for r in rows) / n,
    }


def main() -> None:
    cases = {c["id"]: c for c in json.loads((EVAL_DIR / "rag.json").read_text())}
    out = {}
    for folder in map(Path, sys.argv[1:]):
        rows = load(folder)
        for row in rows:
            row["category"] = cases[row["id"]]["category"]
        out[folder.name] = {"rows": rows}
        for variant in ("off", "on"):
            mine = [r for r in rows if r["variant"] == variant]
            out[folder.name][variant] = summarize(mine)
            out[folder.name][f"{variant}_by_category"] = {
                category: summarize([r for r in mine if r["category"] == category])
                for category in sorted({r["category"] for r in mine})
                if any(r["answerable"] for r in mine if r["category"] == category)
            }
            s = out[folder.name][variant]
            print(f"{folder.name:7s} {variant:3s} 근거충분 {s['evidence_sufficient']:.3f} 일부 {s['evidence_partial']:.3f} "
                  f"없음 {s['evidence_none']:.3f} | 정답 {s['correct']:.3f} 일부 {s['partial']:.3f} 틀림 {s['wrong']:.3f} "
                  f"환각 {s['hallucination']:.3f} | 답없음정답 {s['unanswerable_correct']:.3f} | "
                  f"실패(생성) {s['fail_generation']:.3f} 실패(근거) {s['fail_retrieval']:.3f} | 코드재현율 {s['code_recall']:.3f} "
                  f"근거수 {s['evidence_count']:.1f} {s['seconds']:.1f}s")
    target = Path(sys.argv[1]).parent / "rag_summary.json"
    target.write_text(json.dumps(out, ensure_ascii=False))
    print("saved", target)


if __name__ == "__main__":
    main()
