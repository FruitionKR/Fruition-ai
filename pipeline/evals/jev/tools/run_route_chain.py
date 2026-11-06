"""라우터와 편집 세부 분류를 운영 순서대로 함께 돌려 기준선을 잰다(ADR-0045 0단계). 실제 편집은 하지 않는다.

사용: PYTHONPATH=<dev>/pipeline python run_route_chain.py [--limit N] [--name 결과폴더]
- 라우팅 정답은 잠긴 routing.jsonl 그대로 채점한다. 세부 분류 정답은 없으므로 목표와 형식의 충돌만 센다.
- jev: 라우터·세부 분류 모두 Jev. llm: 둘 다 평가 모델.
"""
import argparse
import dataclasses
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from statistics import median

sys.path.insert(0, os.path.dirname(__file__))
from common import EVAL_DIR, EVAL_MODEL, RUNS, load_keys, results_dir, verify_gold, write_jsonl  # noqa: E402
from run_routing import CountingRouter, build_request, comparable  # noqa: E402

LIST_GOALS = {"checklist", "bullet_list"}


def goal_format_conflict(goal: str | None, formats: list[str]) -> str | None:
    """라우터 목표와 세부 형식이 어긋나면 이유를 돌려준다. 코드가 함께 지킬 수 없는 조합만 본다."""
    if goal in LIST_GOALS and "numbered_list" in formats:
        return "list_goal_with_numbered_list"
    if goal == "convert_format" and not formats:
        return "convert_format_without_format"
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--name", default="route_chain")
    args = parser.parse_args()
    verify_gold()
    load_keys("OPENAI_API_KEY", "TYPESAFE_API_KEY")
    os.environ["AI_ALLOWED_LLM_MODELS"] = "*"
    from app.core.jev_client import JEV_EDIT_DETAIL_ENABLED_ENV, JEV_ROUTING_ENABLED_ENV, build_jev_client
    from app.modules.agent.application.handle_agent_turn import _conversation_context_text
    from app.modules.agent.domain.task_mode import task_mode_route
    from app.modules.agent.infrastructure.chat_completions_turn_router import (
        build_agent_turn_router,
        load_agent_turn_router_prompt,
    )
    from app.modules.agent.infrastructure.jev_turn_router import JevTurnRouter, jev_route_instructions
    from app.modules.markdown_edit.domain.entities import MarkdownEditRequest, MarkdownEditTarget
    from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import build_markdown_editor

    provider, model = EVAL_MODEL
    llm = build_agent_turn_router(provider=provider, model=model)
    os.environ[JEV_ROUTING_ENABLED_ENV] = "true"
    client = build_jev_client(JEV_ROUTING_ENABLED_ENV, interactive=True)
    assert client is not None, "Jev 클라이언트를 만들지 못했습니다"
    prompt = jev_route_instructions(load_agent_turn_router_prompt())
    detail = {}
    for variant, enabled in (("jev", "true"), ("llm", "false")):
        os.environ[JEV_EDIT_DETAIL_ENABLED_ENV] = enabled
        detail[variant] = build_markdown_editor(provider=provider, model=model)._detail_classifier

    cases = [json.loads(line) for line in (EVAL_DIR / "routing.jsonl").read_text().splitlines() if line.strip()]
    cases = cases[: args.limit] if args.limit else cases

    def run(job):
        case, variant, run_index = job
        request = build_request(case)
        fallback = CountingRouter(llm)
        router = JevTurnRouter(client, fallback, prompt) if variant == "jev" else llm
        row = {"id": case["id"], "axis": case.get("axis"), "variant": variant, "run": run_index, "error": None,
               "formats": None, "addition_only": None, "structure_change": None, "conflict": None,
               "detail_seconds": None, "fallback_calls": fallback.calls}
        started = time.monotonic()
        try:
            route = dataclasses.asdict(task_mode_route(router.route(request), request))
        except Exception as exc:  # 계약 실패도 결과로 남긴다.
            return {**row, "ok": False, "got": None, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "route_seconds": round(time.monotonic() - started, 3)}
        got = comparable(route)
        expected = [comparable(case["expected"])] + [comparable(alt) for alt in case.get("acceptable") or []]
        row.update(ok=got in expected, got=got, route_seconds=round(time.monotonic() - started, 3),
                   fallback_calls=fallback.calls)
        # 운영과 같이 문서 편집 라우트만 세부 분류를 부른다.
        if route["document_operation"] == "edit" and route["action"] != "clarify":
            started = time.monotonic()
            found = detail[variant].classify(MarkdownEditRequest(
                instruction=request.message, markdown="평가용 문서",
                target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=1),
                conversation_summary=_conversation_context_text(request), edit_goal=route["edit_goal"],
            ))
            row.update(formats=list(found.requested_formats), addition_only=found.addition_only,
                       structure_change=found.structure_change, detail_seconds=round(time.monotonic() - started, 3),
                       conflict=goal_format_conflict(route["edit_goal"], list(found.requested_formats)))
        return row

    jobs = [(case, variant, r) for case in cases for variant in ("jev", "llm") for r in range(RUNS)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(run, jobs))
    out = results_dir(args.name)
    write_jsonl(out / "rows.jsonl", rows)
    summary = {}
    for variant in ("jev", "llm"):
        mine = [row for row in rows if row["variant"] == variant]
        edits = [row for row in mine if row["detail_seconds"] is not None]
        summary[variant] = {
            "route_ok": sum(row["ok"] for row in mine), "total": len(mine),
            "errors": sum(bool(row["error"]) for row in mine),
            "router_llm_fallback_rows": sum(1 for row in mine if variant == "jev" and row["fallback_calls"]),
            "detail_rows": len(edits), "conflicts": dict(Counter(row["conflict"] for row in edits if row["conflict"])),
            "route_seconds_median": median(row["route_seconds"] for row in mine),
            "detail_seconds_median": median(row["detail_seconds"] for row in edits) if edits else None,
        }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2), "\nsaved to", out)


if __name__ == "__main__":
    main()
