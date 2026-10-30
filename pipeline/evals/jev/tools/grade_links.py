"""개념 평가의 추가 연결(정답 밖 link_to 대상)이 두 정의상 타당한지 Codex로 채점한다.

사용: python grade_links.py <concept rows.jsonl>
- 켬/끔을 가리기 위해 (사례, 연결 대상) 쌍으로 합쳐 한 번씩 채점한다. 자기 연결과 기존 개념이 아닌 대상은 뺀다.
- 결과: rows.jsonl 옆 link_grades.json(쌍별 verdict·note)
"""
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import EVAL_DIR, verify_gold  # noqa: E402
from grade_rag import schema  # noqa: E402

PROMPT = """You are checking concept links in a Korean/English workspace wiki. Grade each item independently.
Each item has an incoming concept and an existing concept (title, aliases, definition). Use only these definitions and
general domain meaning of the two terms; do not assume anything about other concepts.
For each item return:
- verdict: "valid" if a reader of the incoming concept's page would reasonably benefit from a "related concept" link to
  the existing concept because the two concepts have a direct relation (one is part, kind, component, input, measure,
  mechanism, prerequisite, or contrasting alternative of the other, or one definition depends on the other).
  "weak" if they only share a broad topic/domain without a direct relation. "invalid" if unrelated or misleading.
- note: one short Korean sentence explaining the verdict.
Return JSON only, matching the schema. Items:
"""
SCHEMA = schema({"verdict": {"type": "string", "enum": ["valid", "weak", "invalid"]}, "note": {"type": "string"}})


def main() -> None:
    verify_gold()
    rows_path = Path(sys.argv[1])
    data = json.loads((EVAL_DIR / "concept.json").read_text())
    existing = {c["slug"]: c for c in data["existing_concepts"]}
    cases = {c["id"]: c for c in data["cases"]}
    rows = [json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
    pairs = sorted({(r["id"], t) for r in rows for t in r["extra_links"] if t in existing})
    random.Random(20261030).shuffle(pairs)

    def view(concept: dict) -> dict:
        return {k: concept.get(k) for k in ("title", "aliases", "definition") if concept.get(k)}

    items = [
        {"item_id": f"p{i:03d}", "incoming": view(cases[cid]["incoming"]), "existing": view(existing[t])}
        for i, (cid, t) in enumerate(pairs)
    ]
    grades = []
    with tempfile.TemporaryDirectory() as tmp:
        schema_path = Path(tmp) / "schema.json"
        schema_path.write_text(json.dumps(SCHEMA))
        for start in range(0, len(items), 23):
            batch = items[start:start + 23]
            last = Path(tmp) / f"out_{start}.json"
            subprocess.run(
                ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--output-schema", str(schema_path),
                 "-o", str(last), PROMPT + json.dumps(batch, ensure_ascii=False, indent=1)],
                # stdin이 열려 있으면 codex가 추가 입력을 기다리며 멈춘다.
                check=True, cwd=tmp, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900,
            )
            got = json.loads(last.read_text())["grades"]
            assert {g["item_id"] for g in got} == {i["item_id"] for i in batch}, "채점 결과가 항목과 맞지 않습니다"
            grades += got
            print("graded", len(grades), "/", len(items), flush=True)
    by_id = dict(zip((item["item_id"] for item in items), pairs, strict=True))
    out = [{"id": by_id[g["item_id"]][0], "target": by_id[g["item_id"]][1], "verdict": g["verdict"], "note": g["note"]} for g in grades]
    (rows_path.parent / "link_grades.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
