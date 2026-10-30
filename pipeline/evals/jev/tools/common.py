"""Jev 켬/끔 평가 실행기 공통: 정답 lock 대조, 키 로드, 결과 저장."""
import hashlib
import json
import os
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]
HAIKU = ("claude", "claude-haiku-4-5-20251001")
RUNS = 3


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
