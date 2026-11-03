"""Jev 켬/끔 평가 실행기 공통: 정답 lock 대조, 키 로드, 결과 저장."""
import hashlib
import json
import os
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]
# 모든 평가의 LLM. 모델이 섞이면 결과를 한 모델 기준으로 보고할 수 없다.
EVAL_MODEL = ("openai", "gpt-6-luna")
RUNS = 3
# 채점자. 실험 모델(gpt-6-luna)과 다른 모델이 판정한다.
JUDGE_MODEL = "claude-opus-5-5"


def judge(prompt: str, schema: dict, cwd: Path, timeout: int, tools: str = "") -> dict:
    """Claude Code 헤드리스(`claude -p`)로 채점하고 JSON 스키마에 맞는 결과를 돌려준다. tools가 비면 도구 없이 판정한다."""
    import subprocess

    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", JUDGE_MODEL, "--output-format", "json", "--json-schema", json.dumps(schema),
         "--tools", tools, "--no-session-persistence"],
        # stdin이 열려 있으면 추가 입력을 기다리며 멈춘다.
        check=True, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout,
    )
    result = json.loads(proc.stdout)
    assert not result.get("is_error") and result.get("structured_output"), f"채점 실패: {result.get('result', '')[:200]}"
    return result["structured_output"]


def verify_gold() -> dict:
    """gold.lock.json의 sha256과 파일이 다르면 실행하지 않는다."""
    lock = json.loads((EVAL_DIR / "gold.lock.json").read_text())
    for name, meta in lock["files"].items():
        if name == "corpus":
            for file, digest in meta.items():
                actual = hashlib.sha256((EVAL_DIR / "corpus" / file).read_bytes()).hexdigest()
                assert actual == digest, f"corpus/{file} 정답 파일이 바뀌었습니다"
            continue
        actual = hashlib.sha256((EVAL_DIR / name).read_bytes()).hexdigest()
        assert actual == meta["sha256"], f"{name} 정답 파일이 바뀌었습니다"
    return lock


def load_keys(*names: str) -> None:
    """키 값은 출력하지 않고 환경변수로만 올린다. 이미 있는 환경변수는 그대로 쓰고, 없으면 JEV_EVAL_ENV_FILE에서 읽는다."""
    env_file = os.environ.get("JEV_EVAL_ENV_FILE")
    lines = Path(env_file).read_text().splitlines() if env_file and not all(os.environ.get(n) for n in names) else []
    for line in lines:
        key, _, value = line.partition("=")
        if key in names and value and not os.environ.get(key):
            os.environ[key] = value.strip().strip('"').strip("'")
    missing = [name for name in names if not os.environ.get(name)]
    assert not missing, f"키 없음: {missing}"


def results_dir(name: str) -> Path:
    path = EVAL_DIR / "results" / time.strftime("%Y-%m-%d") / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
