"""PDF Converter 유형별 평가 실행기: 고정한 PDF를 운영 converter 이미지의 변환 함수(process_pdf)로 Markdown으로 바꾼다.

사용: python run_pdf.py --image fruition-converter:<커밋> [--limit N] [--ids a,b]
      python run_pdf.py --write-lock      # 정답 검수가 끝난 뒤 한 번만. cases.json·gold는 파일, sources는 내용 기준 sha256으로 고정한다
결과: results/<날짜>/rows.jsonl(사례·회차별 상태·시간), results/<날짜>/md/<id>.<회차>.md
- 변환은 운영과 같은 crop-first + selective repair이며 repair 모델은 평가 모델(gpt-6-luna)이다.
- 처리 시간(seconds)은 컨테이너 시작부터 변환 끝까지이며, 변환을 동시에 WORKERS개 돌려 서로 영향을 받는다.
- 컨테이너 env는 실행 중인 converter 컨테이너(--env-from)에서 출력 없이 읽어 임시 파일로만 넘긴다.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]
EVAL_MODEL = ("openai", "gpt-6-luna")
RUNS = 3
WORKERS = 3

INNER = """
import json, sys, time
from pathlib import Path
from converter_app import process_pdf
started = time.monotonic()
result = process_pdf(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
Path(sys.argv[4]).write_text(result["markdown"], encoding="utf-8")
print(json.dumps({"seconds": round(time.monotonic() - started, 1), "repair_summary": result["repair_summary"]}))
"""


def locked_files() -> list[Path]:
    return [EVAL_DIR / "cases.json", *sorted((EVAL_DIR / "gold").glob("*.json")), *sorted((EVAL_DIR / "sources").glob("*.pdf"))]


def digest(path: Path) -> str:
    """원문 PDF는 저장할 때마다 문서 ID·생성 시각이 바뀌므로 내용(쪽별 글자와 이미지)으로 센다."""
    if path.suffix != ".pdf":
        return hashlib.sha256(path.read_bytes()).hexdigest()
    import fitz

    h, doc = hashlib.sha256(), fitz.open(path)
    for page in doc:
        h.update(page.get_text().encode())
        h.update(b"\0")
        for image in page.get_images(full=True):
            h.update(hashlib.sha256(doc.extract_image(image[0])["image"]).digest())
        h.update(b"\1")
    return h.hexdigest()


def verify_gold() -> None:
    """gold.lock.json과 파일이 다르면 실행하지 않는다."""
    lock = json.loads((EVAL_DIR / "gold.lock.json").read_text())
    files = {str(p.relative_to(EVAL_DIR)): digest(p) for p in locked_files()}
    assert files == lock["files"], "정답·원문 파일이 고정한 것과 다릅니다"


def container_env(name: str) -> str:
    env = subprocess.run(["docker", "inspect", name, "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
                         check=True, capture_output=True, text=True).stdout
    keep = [line for line in env.splitlines() if line and not line.startswith(("PATH=", "PYTHON", "GPG_KEY=", "LANG="))]
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(keep) + "\n")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image")
    parser.add_argument("--env-from", default="fruition-markitdown")
    parser.add_argument("--network", default="jev-flow_default")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids")
    parser.add_argument("--write-lock", action="store_true")
    args = parser.parse_args()
    if args.write_lock:
        files = {str(p.relative_to(EVAL_DIR)): digest(p) for p in locked_files()}
        lock_path = EVAL_DIR / "gold.lock.json"
        # 개정 이력(revisions) 등 다른 항목은 그대로 두고 files만 바꾼다.
        lock = json.loads(lock_path.read_text()) if lock_path.exists() else {}
        lock["files"] = files
        lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=1) + "\n")
        print("locked", len(files))
        return
    if not args.image:
        parser.error("--image가 필요합니다")
    verify_gold()
    cases = json.loads((EVAL_DIR / "cases.json").read_text())
    if args.ids:
        cases = [c for c in cases if c["id"] in args.ids.split(",")]
    cases = cases[: args.limit] if args.limit else cases

    out = EVAL_DIR / "results" / time.strftime("%Y-%m-%d")
    (out / "md").mkdir(parents=True, exist_ok=True)
    env_file = container_env(args.env_from)
    def convert(job):
        case, run = job
        md_name = f"{case['id']}.{run}.md"
        command = ["docker", "run", "--rm", "--network", args.network, "--env-file", env_file,
                   "-v", f"{EVAL_DIR / 'sources'}:/in:ro", "-v", f"{out / 'md'}:/out", args.image,
                   "python", "-c", INNER, f"/in/{case['file']}", *EVAL_MODEL, f"/out/{md_name}"]
        started = time.monotonic()
        proc = subprocess.run(command, capture_output=True, text=True)
        row = {"id": case["id"], "type": case["type"], "run": run, "image": args.image,
               "model": ":".join(EVAL_MODEL), "seconds": round(time.monotonic() - started, 1)}
        if proc.returncode == 0:
            row.update(json.loads(proc.stdout.strip().splitlines()[-1]), status="completed", markdown=f"md/{md_name}")
        else:
            row.update(status="failed", error=proc.stderr.strip().splitlines()[-1][:300] if proc.stderr.strip() else "unknown")
        print(case["id"], run, row["status"], row["seconds"], flush=True)
        return row

    rows = []
    try:
        # 변환마다 컨테이너 하나. docling이 메모리를 많이 써서 동시에 WORKERS개만 돌린다.
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            rows = list(executor.map(convert, [(case, run) for case in cases for run in range(RUNS)]))
    finally:
        os.remove(env_file)
        # 같은 날 일부 사례만 다시 돌려도 다른 사례 결과가 남도록, 같은 (사례, 회차)만 바꿔 쓴다.
        rows_path = out / "rows.jsonl"
        kept = [json.loads(line) for line in rows_path.read_text().splitlines()] if rows_path.exists() else []
        done = {(r["id"], r["run"]) for r in rows}
        merged = [r for r in kept if (r["id"], r["run"]) not in done] + rows
        rows_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in merged))
    print("completed", sum(r["status"] == "completed" for r in rows), "/", len(rows), "saved", out)


if __name__ == "__main__":
    sys.exit(main())
