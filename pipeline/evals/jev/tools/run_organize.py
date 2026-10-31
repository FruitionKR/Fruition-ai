"""문서 관리 평가. 폴더 정리 계획을 만들어 폴더 트리에 적용해 보고 최종 배치를 정답과 비교한다. 폴더 조회는 고른 폴더를 비교한다.

사용: PYTHONPATH=<dev>/pipeline python run_organize.py [--limit N]
- 계획·조회 모델: Claude Haiku. 실행(백엔드 반영)은 하지 않고 계획만 메모리 트리에 적용한다.
- 범위가 있는 사례는 운영 worker처럼 scoped_hierarchy로 잘라 계획을 만들고, 적용은 전체 트리에서 한다.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))
from common import EVAL_DIR, HAIKU, RUNS, load_keys, results_dir, verify_gold, write_jsonl  # noqa: E402

PARENT_KEYS = {"create_folder": "parent_folder_id", "move_folder": "parent_folder_id", "move_document": "folder_id"}
NAME_KEYS = {"create_folder": "name", "rename_folder": "name", "rename_document": "display_name"}


def replay(hierarchy: list[dict], operations: list[dict]) -> dict[str, dict]:
    """계획 작업을 순서대로 적용한 트리. 새 폴더는 작업 id를 id로 쓴다."""
    tree = {item["id"]: {"type": item["type"], "name": item["name"], "parent": item["parent_id"], "new": False} for item in hierarchy}

    def ref(value):
        return value["$operation_result"] if isinstance(value, dict) else value

    for op in sorted(operations, key=lambda item: item["sequence"]):
        args = op["arguments"]
        if op["tool_name"] == "create_folder":
            tree[op["id"]] = {"type": "folder", "name": args["name"], "parent": ref(args["parent_folder_id"]), "new": True}
            continue
        target = tree[ref(op["target_id"])]
        if op["tool_name"] in PARENT_KEYS:
            target["parent"] = ref(args[PARENT_KEYS[op["tool_name"]]])
        if op["tool_name"] in NAME_KEYS:
            target["name"] = args[NAME_KEYS[op["tool_name"]]]
    return tree


def _same(text: str) -> str:
    return "".join(str(text).split()).lower()


def _ancestors(tree: dict, folder_id: str | None) -> list[str | None]:
    chain = [folder_id]
    while folder_id is not None:
        folder_id = tree[folder_id]["parent"]
        chain.append(folder_id)
    return chain


def _parent_ok(tree: dict, parent: str | None, spec) -> bool:
    if not isinstance(spec, dict):
        return parent == spec
    folder = tree.get(parent) if parent else None
    if folder is None or not folder["new"] or _same(folder["name"]) not in {_same(n) for n in spec["new_folder_name_any_of"]}:
        return False
    return "parent" not in spec or _parent_ok(tree, folder["parent"], spec["parent"])


def check_plan(expected: dict, original: list[dict], tree: dict, operations: list[dict], summary: str) -> list[str]:
    failures = []
    before = {item["id"]: item for item in original}
    for item_id, spec in (expected.get("final_parent") or {}).items():
        if not _parent_ok(tree, tree[item_id]["parent"], spec):
            failures.append(f"배치 틀림: {item_id}")
    parents = []
    for group in expected.get("groups") or []:
        group_parents = {tree[item_id]["parent"] for item_id in group}
        if len(group_parents) != 1:
            failures.append(f"묶음이 갈라짐: {group}")
        parent = next(iter(group_parents))
        parents.append(parent)
        if expected.get("groups_parent_not_root") and parent is None:
            failures.append(f"묶음이 루트에 있음: {group}")
        if expected.get("groups_within") and expected["groups_within"] not in _ancestors(tree, parent):
            failures.append(f"묶음이 범위 밖: {group}")
    if expected.get("distinct_groups") and len(set(parents)) != len(parents):
        failures.append("서로 다른 묶음이 같은 폴더에 있음")
    for item_id, names in (expected.get("renamed") or {}).items():
        if _same(tree[item_id]["name"]) not in {_same(n) for n in names}:
            failures.append(f"이름 틀림: {item_id}")
    for item_id in expected.get("unchanged") or []:
        if tree[item_id]["parent"] != before[item_id]["parent_id"] or tree[item_id]["name"] != before[item_id]["name"]:
            failures.append(f"건드리면 안 되는 항목이 바뀜: {item_id}")
    for item_id in expected.get("names_unchanged") or []:
        if tree[item_id]["name"] != before[item_id]["name"]:
            failures.append(f"이름이 바뀜: {item_id}")
    if expected.get("max_operations") and len(operations) > expected["max_operations"]:
        failures.append(f"작업 수 초과: {len(operations)}")
    if expected.get("summary_mentions_any") and not any(word in summary for word in expected["summary_mentions_any"]):
        failures.append("요약에 알려야 할 내용 없음")
    return failures


def judge(case: dict, outcome: str, tree: dict | None, operations: list[dict], summary: str) -> tuple[bool, list[str]]:
    """기대 답이나 허용 답 중 하나와 맞으면 성공. 실패 사유는 기대 답 기준으로 남긴다."""
    reasons = None
    for expected in [case["expected"], *case.get("acceptable", [])]:
        if expected["kind"] == "clarify":
            failures = [] if outcome == "clarify" else ["되물어야 함"]
        elif outcome != "plan":
            failures = [f"계획 대신 {outcome}"]
        else:
            failures = check_plan(expected, case["hierarchy"], tree, operations, summary)
        if not failures:
            return True, []
        reasons = reasons if reasons is not None else failures
    return False, reasons


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--name", default="organize", help="결과 폴더 이름(수정 전후 비교용)")
    args = parser.parse_args()
    verify_gold()
    data = json.loads((EVAL_DIR / "organize.json").read_text())
    organize, lookup = data["organize"], data["lookup"]
    if args.limit:
        organize, lookup = organize[: args.limit], lookup[: args.limit]
    load_keys("ANTHROPIC_API_KEY")
    os.environ["AI_ALLOWED_LLM_MODELS"] = "*"
    from app.modules.agent.domain.entities import AgentTurnRequest
    from app.modules.agent.domain.folder_lookup import folder_entries
    from app.modules.agent.infrastructure.chat_completions_folder_resolver import build_folder_reference_resolver
    from app.modules.agent_run.domain.plan import AgentPlanIntentClarificationRequired
    from app.modules.agent_run.infrastructure.agent_worker import scoped_hierarchy
    from app.modules.agent_run.infrastructure.chat_completions_plan_generator import build_plan_generator

    planner = build_plan_generator(provider=HAIKU[0], model=HAIKU[1])
    resolver = build_folder_reference_resolver(provider=HAIKU[0], model=HAIKU[1])

    def run_organize(job):
        case, run_index = job
        started = time.monotonic()
        tree, operations, summary, error = None, [], "", None
        try:
            plan = planner.generate(
                run_id=f"eval-{case['id']}-{run_index}", plan_id=f"plan-{case['id']}", version=1,
                instruction=case["instruction"], routing_action="folder_organize",
                hierarchy=scoped_hierarchy(case["hierarchy"], case.get("target_folder_id")),
                skill_instructions=None, allowed_tools=None, target_folder_id=case.get("target_folder_id"),
            )
            operations = [
                {"id": op.id, "sequence": op.sequence, "tool_name": op.tool_name, "target_id": op.target_id, "arguments": op.arguments}
                for op in plan.operations
            ]
            summary, outcome = plan.summary, "plan"
            tree = replay(case["hierarchy"], operations)
        except AgentPlanIntentClarificationRequired:
            outcome = "clarify"
        except Exception as exc:  # 검증 실패·범위 밖 계획도 결과(실패)로 남긴다.
            outcome, error = "error", f"{type(exc).__name__}: {str(exc)[:300]}"
        ok, failures = judge(case, outcome, tree, operations, summary)
        return {
            "id": case["id"], "category": case["category"], "run": run_index, "outcome": outcome, "ok": ok,
            "failures": failures, "summary": summary, "operations": operations, "error": error,
            "seconds": round(time.monotonic() - started, 3),
        }

    def run_lookup(job):
        case, run_index = job
        started = time.monotonic()
        try:
            got, error = sorted(resolver.resolve(AgentTurnRequest(message=case["request"]), folder_entries(case["tree"]))), None
        except Exception as exc:
            got, error = None, f"{type(exc).__name__}: {str(exc)[:300]}"
        accepted = [sorted(case["expected_folder_ids"]), *(sorted(alt) for alt in case.get("acceptable", []))]
        return {
            "id": case["id"], "category": case["category"], "run": run_index, "outcome": "lookup", "ok": got in accepted,
            "failures": [] if got in accepted else [f"고른 폴더: {got}"], "got": got, "error": error,
            "seconds": round(time.monotonic() - started, 3),
        }

    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(run_organize, [(c, r) for c in organize for r in range(RUNS)]))
        rows += list(executor.map(run_lookup, [(c, r) for c in lookup for r in range(RUNS)]))
    out = results_dir(args.name)
    write_jsonl(out / "rows.jsonl", rows)
    for name, mine in (("정리", [r for r in rows if r["outcome"] != "lookup"]), ("조회", [r for r in rows if r["outcome"] == "lookup"])):
        print(name, sum(r["ok"] for r in mine), "/", len(mine), "errors", sum(bool(r["error"]) for r in mine))
    print("saved to", out)


if __name__ == "__main__":
    main()
