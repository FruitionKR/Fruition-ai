"""답변을 원자 주장으로 나누고 각 주장을 corpus 원문과 대조한다(23p RAG/Wiki 주장 단위 검증).

사용: python grade_claims.py <out_dir> <rows.jsonl>[:variant] ...
  예: python grade_claims.py ../results/<날짜>/claims ../results/<날짜>/rag/rows.jsonl:on ../results/<날짜>/rag_baseline/rows.jsonl
- 켬/끔·대조군을 가리고 순서를 섞어 Codex(읽기 전용)가 corpus 파일을 직접 읽어 판정한다. 같은 질문의 같은 답변은 한 번만 본다.
- 판정: supported(원문이 뒷받침) / unsupported(원문에 없음) / contradicted(원문과 다름).
결과: <out_dir>/claim_grades.jsonl, claim_map.json, claims_summary.json
"""
import collections
import json
import random
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import EVAL_DIR, verify_gold  # noqa: E402
from grade_rag import schema  # noqa: E402

BATCH = 5
PROMPT = """You are verifying answers from a Korean/English Q&A system against the original source documents.
The source documents are the Markdown files in the current directory (read them with shell commands such as grep, sed
or cat; do not use any other knowledge). For each item:
1. Split the answer into atomic factual claims: each claim states one checkable fact (a number, date, name, rule,
   condition, definition or relation). Skip greetings, hedges, statements that information is unavailable, and
   restatements of the question.
2. For each claim, search the source documents and decide:
   - "supported": a source passage states it (paraphrase or translation is fine);
   - "contradicted": a source passage states something incompatible;
   - "unsupported": no source passage states it.
   Give the file name and a short exact quote (at most 200 characters) for supported or contradicted claims.
Return JSON only, matching the schema. Items:
"""
SCHEMA = schema({
    "claims": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["claim", "verdict", "source_file", "quote"],
        "properties": {
            "claim": {"type": "string"},
            "verdict": {"type": "string", "enum": ["supported", "unsupported", "contradicted"]},
            "source_file": {"type": "string"},
            "quote": {"type": "string"},
        },
    }},
})


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    verify_gold()
    cases = {c["id"]: c for c in json.loads((EVAL_DIR / "rag.json").read_text())}
    rng = random.Random(20261030)
    items, mapping = {}, collections.defaultdict(list)
    for spec in sys.argv[2:]:
        path, _, variant = spec.partition(":")
        for row in map(json.loads, Path(path).read_text().splitlines()):
            if variant and row["variant"] != variant or not row.get("answer"):
                continue
            key = (row["id"], row["answer"])
            if key not in items:
                items[key] = {"item_id": uuid.UUID(int=rng.getrandbits(128)).hex[:12], "question": cases[row["id"]]["question"], "answer": row["answer"]}
            mapping[items[key]["item_id"]].append({"id": row["id"], "variant": row["variant"], "run": row["run"], "source": path})
    pending = list(items.values())
    rng.shuffle(pending)
    grades_path = out / "claim_grades.jsonl"
    done = {json.loads(line)["item_id"] for line in grades_path.read_text().splitlines()} if grades_path.exists() else set()
    pending = [item for item in pending if item["item_id"] not in done]
    (out / "claim_map.json").write_text(json.dumps(mapping, ensure_ascii=False))
    print("답변", len(items), "남은 채점", len(pending), flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "corpus"
        shutil.copytree(EVAL_DIR / "corpus", work)
        schema_path = Path(tmp) / "schema.json"
        schema_path.write_text(json.dumps(SCHEMA))
        for start in range(0, len(pending), BATCH):
            batch = pending[start:start + BATCH]
            last = Path(tmp) / f"out_{start}.json"
            subprocess.run(
                ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--output-schema", str(schema_path),
                 "-o", str(last), PROMPT + json.dumps(batch, ensure_ascii=False, indent=1)],
                # stdin이 열려 있으면 codex가 추가 입력을 기다리며 멈춘다.
                check=True, cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=1800,
            )
            grades = json.loads(last.read_text())["grades"]
            assert {g["item_id"] for g in grades} == {i["item_id"] for i in batch}, "채점 결과가 항목과 맞지 않습니다"
            with grades_path.open("a") as f:
                f.writelines(json.dumps(g, ensure_ascii=False) + "\n" for g in grades)
            print("graded", start + len(batch), "/", len(pending), flush=True)
    summarize(out, mapping)


def summarize(out: Path, mapping: dict) -> None:
    """실행(행) 단위로 주장 수를 센다. 같은 답변을 여러 실행이 냈으면 실행마다 센다."""
    totals = collections.defaultdict(collections.Counter)
    for grade in map(json.loads, (out / "claim_grades.jsonl").read_text().splitlines()):
        verdicts = collections.Counter(claim["verdict"] for claim in grade["claims"])
        for key in mapping[grade["item_id"]]:
            totals[key["variant"]].update(verdicts)
            totals[key["variant"]]["answers"] += 1
    summary = {}
    for variant, counter in totals.items():
        claims = counter["supported"] + counter["unsupported"] + counter["contradicted"]
        summary[variant] = {**counter, "claims": claims, "unsupported_rate": (counter["unsupported"] + counter["contradicted"]) / max(1, claims)}
        print(variant, summary[variant])
    (out / "claims_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
