"""새 문서·편집·화면 표시 요청의 작업 종류를 Jev·LLM 라우터로 잰다. 평가셋: evals/agent_route_create_edit.jsonl

사용: PYTHONPATH=<dev>/pipeline python run_route_create_edit.py [--limit N] [--name 결과폴더]
작성자가 실패 사례를 본 뒤 쓴 문장이라 판단용 분리 평가셋이 아니다. 회귀 확인에 쓴다.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))
from common import EVAL_DIR, EVAL_MODEL, RUNS, load_keys, results_dir, write_jsonl  # noqa: E402
from run_routing import build_request  # noqa: E402

DOCUMENT = (
    "# 운영 안내\n\n서비스 운영 절차와 담당자를 정리한 문서다.\n\n## 복사본 만드는 방법\n\n문서를 복사해 쓴다.\n\n"
    "## 새 문서 작성 규칙\n\n템플릿을 쓴다.\n\n## 영어 버전 출시 일정\n\n이번 달 말이다.\n\n"
    "| 항목 | 담당 | 마감 |\n| --- | --- | --- |\n| 점검 | 김 | 3일 |\n"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--name", default="route_create_edit")
    args = parser.parse_args()
    load_keys("OPENAI_API_KEY", "TYPESAFE_API_KEY")
    os.environ["AI_ALLOWED_LLM_MODELS"] = "*"
    os.environ["JEV_ROUTING_ENABLED"] = "true"
    from app.core.jev_client import build_jev_client
    from app.modules.agent.domain.task_mode import task_mode_route
    from app.modules.agent.infrastructure.chat_completions_turn_router import (
        build_agent_turn_router,
        load_agent_turn_router_prompt,
    )
    from app.modules.agent.infrastructure.jev_turn_router import JevTurnRouter, jev_route_instructions

    llm = build_agent_turn_router(provider=EVAL_MODEL[0], model=EVAL_MODEL[1])
    client = build_jev_client("JEV_ROUTING_ENABLED", interactive=True)
    assert client is not None, "Jev 클라이언트를 만들지 못했습니다"
    jev = JevTurnRouter(client, llm, jev_route_instructions(load_agent_turn_router_prompt()))
    path = EVAL_DIR.parent / "agent_route_create_edit.jsonl"
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    cases = cases[: args.limit] if args.limit else cases

    def run(job):
        case, variant, run_index = job
        request = build_request({
            "message": case["message"], "context": {"mode": "task", "active_markdown": {"markdown": DOCUMENT}},
        })
        route = task_mode_route((jev if variant == "jev" else llm).route(request), request)
        return {"id": case["id"], "variant": variant, "run": run_index, "ok": route.action in case["action"],
                "action": route.action, "edit_goal": route.edit_goal, "notice_kind": route.notice_kind}

    jobs = [(case, variant, r) for case in cases for variant in ("jev", "llm") for r in range(RUNS)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(run, jobs))
    out = results_dir(args.name)
    write_jsonl(out / "rows.jsonl", rows)
    for variant in ("jev", "llm"):
        mine = [row for row in rows if row["variant"] == variant]
        print(variant, sum(row["ok"] for row in mine), "/", len(mine))
    print("saved to", out)


if __name__ == "__main__":
    main()
