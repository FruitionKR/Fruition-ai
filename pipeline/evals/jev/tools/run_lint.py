"""평가 워크스페이스에서 운영 경로로 Wiki lint를 실행한다: 백엔드 API → Kafka → 유지보수 워커.

사용: python run_lint.py <workspace.json> [--dry-run]
- 운영처럼 승격을 반영(materialize_promotions)한다. --dry-run이면 결과만 보고 Wiki를 바꾸지 않는다.
- 결과: workspace.json 옆 lint[_dry].json(백엔드가 돌려준 실행 결과 전체)
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rag_prepare import ACCESS, DOCUMENT, call  # noqa: E402

POLL_SECONDS = 10
TIMEOUT_SECONDS = 3600


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    path = Path(args.workspace)
    state = json.loads(path.read_text())
    token = call("POST", f"{ACCESS}/api/auth/login", {"email": state["email"], "password": "jev-eval-password"})["access_token"]
    ws = state["workspace_id"]
    queued = call("POST", f"{DOCUMENT}/api/workspaces/{ws}/wiki/maintenance/lint",
                  {"dry_run": args.dry_run, "materialize_promotions": not args.dry_run}, token, expected=(202,))
    print("queued", queued, flush=True)
    started = time.monotonic()
    while True:
        time.sleep(POLL_SECONDS)
        run = call("GET", f"{DOCUMENT}/api/workspaces/{ws}/wiki/maintenance/runs/{queued['run_id']}", None, token)
        if run.get("status") not in {"queued", "running", "pending"}:
            break
        assert time.monotonic() - started < TIMEOUT_SECONDS, "lint가 시간 안에 끝나지 않았습니다"
    run["seconds"] = round(time.monotonic() - started, 1)
    run["operation_id"] = queued.get("operation_id")
    out = path.parent / ("lint_dry.json" if args.dry_run else "lint.json")
    out.write_text(json.dumps(run, ensure_ascii=False, indent=1))
    print("status", run.get("status"), "seconds", run["seconds"], "saved", out)


if __name__ == "__main__":
    main()
