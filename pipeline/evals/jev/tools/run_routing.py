"""라우팅 Jev 켬/끔 비교. 운영 흐름(라우터 → 작업 탭 후처리)을 그대로 부르고 정답과 정확히 비교한다.

사용: PYTHONPATH=<dev>/pipeline python run_routing.py [--dry-run] [--limit N]
- 끔: LLM 라우터(Haiku)
- 켬: JEV_ROUTING_ENABLED=true. 스킬 후보가 겹치거나 Jev가 실패하면 운영처럼 같은 LLM 라우터로 넘어가고, 그 횟수를 센다.
"""
import argparse
import dataclasses
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))
from common import EVAL_DIR, HAIKU, RUNS, load_keys, results_dir, verify_gold, write_jsonl  # noqa: E402

from app.modules.agent.domain.entities import (  # noqa: E402
    ActiveMarkdownContext,
    AgentConversationContext,
    AgentTurnRequest,
    SkillCandidate,
)
from app.modules.agent.domain.task_mode import task_mode_route  # noqa: E402
from app.modules.markdown_edit.domain.entities import MarkdownEditTarget  # noqa: E402
from app.modules.query.domain.entities import ConversationAgentRoute, ConversationMessage  # noqa: E402
from app.modules.skill.domain.entities import SkillDraftSourceRun  # noqa: E402

FIELDS = (
    "action", "retrieval_source", "document_operation", "persist", "notice_kind",
    "edit_goal", "edit_operation", "edit_destination", "selected_skill_id", "skill_candidates",
)
# 라우터는 선택된 완료 작업이 있는지만 본다. 내용은 자리표시값이다.
SOURCE = SkillDraftSourceRun(**{field.name: "x" for field in dataclasses.fields(SkillDraftSourceRun)})
ROUTE_FIELDS = {f.name for f in dataclasses.fields(ConversationAgentRoute)}


def build_request(case: dict) -> AgentTurnRequest:
    ctx = case["context"]
    am = ctx.get("active_markdown")
    messages = tuple(
        ConversationMessage(
            role=m["role"], content=m["content"], action=m.get("action"),
            agent_route=ConversationAgentRoute(**{k: v for k, v in m["agent_route"].items() if k in ROUTE_FIELDS})
            if m.get("agent_route") else None,
        )
        for m in ctx.get("recent_messages", [])
    )
    return AgentTurnRequest(
        message=case["message"],
        document_id="evaluation-document" if am else None,
        active_markdown_context=ActiveMarkdownContext(
            am["markdown"], MarkdownEditTarget(**am["target"]) if am.get("target") else None
        ) if am else None,
        conversation_context=AgentConversationContext(recent_messages=messages) if messages else None,
        allow_web_search=ctx.get("allow_web_search"),
        mode=ctx.get("mode"),
        skill_mode=ctx.get("skill_mode", "auto"),
        available_skills=tuple(
            SkillCandidate(**{**s, "capabilities": tuple(s["capabilities"])}) for s in ctx.get("available_skills", [])
        ),
        skill_draft_sources=(SOURCE,) if ctx.get("has_selected_completed_work") else (),
    )


def comparable(route: dict) -> dict:
    value = {key: route.get(key) for key in FIELDS}
    value["skill_candidates"] = sorted(value["skill_candidates"] or [])
    return value


class CountingRouter:
    """Jev 켬에서 운영 코드가 LLM 라우터로 넘긴 횟수를 센다."""

    def __init__(self, router) -> None:
        self._router = router
        self.calls = 0

    def route(self, request):
        self.calls += 1
        return self._router.route(request)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    verify_gold()
    cases = [json.loads(line) for line in (EVAL_DIR / "routing.jsonl").read_text().splitlines() if line.strip()]
    cases = cases[: args.limit] if args.limit else cases
    requests = {case["id"]: build_request(case) for case in cases}
    if args.dry_run:
        from app.modules.agent.infrastructure.jev_turn_router import route_options

        for case in cases:
            options = route_options(requests[case["id"]])
            print(case["id"], "Jev 선택지", len(options), "| 정답", case["expected"]["action"])
        return

    load_keys("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY")
    os.environ["AI_ALLOWED_LLM_MODELS"] = "*"
    from app.modules.agent.infrastructure.chat_completions_turn_router import build_agent_turn_router
    from app.core.jev_client import JEV_ROUTING_ENABLED_ENV, build_jev_client
    from app.modules.agent.infrastructure.chat_completions_turn_router import load_agent_turn_router_prompt
    from app.modules.agent.infrastructure.jev_turn_router import JevTurnRouter

    llm = build_agent_turn_router(provider=HAIKU[0], model=HAIKU[1])
    os.environ[JEV_ROUTING_ENABLED_ENV] = "true"
    # 운영 with_jev_routing과 같은 클라이언트·프롬프트로 만든다. 문항마다 만들어 LLM으로 넘어간 횟수를 정확히 센다.
    client = build_jev_client(JEV_ROUTING_ENABLED_ENV, interactive=True)
    assert client is not None, "Jev 클라이언트를 만들지 못했습니다"
    prompt = load_agent_turn_router_prompt()

    def run(job):
        case, variant, run_index = job
        request = requests[case["id"]]
        fallback = CountingRouter(llm)
        router = JevTurnRouter(client, fallback, prompt) if variant == "on" else llm
        started = time.monotonic()
        try:
            route = dataclasses.asdict(task_mode_route(router.route(request), request))
            got, error = comparable(route), None
        except Exception as exc:  # 계약 실패 등도 결과로 남긴다.
            route, got, error = None, None, f"{type(exc).__name__}: {str(exc)[:200]}"
        expected = [comparable(case["expected"])] + [comparable(alt) for alt in case.get("acceptable") or []]
        return {
            "id": case["id"], "axis": case.get("axis"), "variant": variant, "run": run_index,
            "ok": got in expected, "got": got, "reason": (route or {}).get("reason"), "error": error,
            # 켬에서 1이면 운영 코드가 그 문항을 LLM 라우터로 넘긴 것이다.
            "fallback_calls": fallback.calls if variant == "on" else None,
            "seconds": round(time.monotonic() - started, 3),
        }

    jobs = [(case, variant, r) for case in cases for variant in ("off", "on") for r in range(RUNS)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(run, jobs))
    out = results_dir("routing")
    write_jsonl(out / "rows.jsonl", rows)
    for variant in ("off", "on"):
        mine = [row for row in rows if row["variant"] == variant]
        print(variant, sum(row["ok"] for row in mine), "/", len(mine), "errors", sum(bool(row["error"]) for row in mine))
    print("fallback LLM rows in on:", sum(1 for row in rows if row["fallback_calls"]), "| saved to", out)


if __name__ == "__main__":
    main()
