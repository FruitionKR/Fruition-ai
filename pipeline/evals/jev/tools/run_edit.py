"""문서 편집 평가. 운영과 같은 편집 use case로 사례를 실행하고 결과에 코드 검사를 적용한다.

사용: PYTHONPATH=<dev>/pipeline python run_edit.py [--limit N] [--ids edit-01,edit-02] [--model provider:model]
- 편집 모델: 기본 Claude Haiku. 결과 판정(Codex)은 grade_features.py가 한다.
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))
from common import EVAL_DIR, HAIKU, RUNS, load_keys, results_dir, verify_gold, write_jsonl  # noqa: E402

LINE_BREAK = re.compile(r"\r\n|\r|\n")


def split_result(original: str, result: str, case: dict) -> tuple[bool, list[str]]:
    """대상 밖 줄이 그대로인지와, 바뀌거나 추가된 줄을 돌려준다."""
    before, after = LINE_BREAK.split(original), LINE_BREAK.split(result)
    target = case["target"]
    if case["edit_operation"] == "insert_after":
        keep_head, keep_tail = before[: target["end_line"]], before[target["end_line"]:]
    else:
        keep_head, keep_tail = before[: target["start_line"] - 1], before[target["end_line"]:]
    tail_start = len(after) - len(keep_tail)
    unchanged = after[: len(keep_head)] == keep_head and tail_start >= len(keep_head) and after[tail_start:] == keep_tail
    changed = after[len(keep_head):tail_start] if unchanged else after
    return unchanged, changed


def format_ok(kind: str | None, lines: list[str]) -> bool:
    text = "\n".join(lines)
    if kind is None:
        return True
    if kind == "checklist":
        return bool(re.search(r"^\s*[-*] \[[ xX]\] ", text, re.M))
    if kind == "bullet_list":
        return bool(re.search(r"^\s*([-*+]|\d+\.) ", text, re.M))
    if kind == "table":
        return bool(re.search(r"^\s*\|?\s*:?-{3,}", text, re.M))
    hangul = len(re.findall(r"[가-힣]", text))
    letters = hangul + len(re.findall(r"[A-Za-z]", text))
    if kind == "english":
        return letters > 0 and hangul / letters < 0.05
    if kind == "korean":
        return hangul > 0
    raise ValueError(kind)


def check(case: dict, original: str, result: str) -> dict:
    checks = case["checks"]
    unchanged, changed = split_result(original, result, case)
    changed_text = "\n".join(changed)
    # 모든 사례가 변경을 요청하므로 원문과 같으면 실패다.
    failures = ["결과가 원문과 같음"] if result == original else []
    if checks.get("outside_target_unchanged") and not unchanged:
        failures.append("대상 밖 줄이 바뀜")
    failures += [f"보존 문자열 없음: {s}" for s in checks.get("preserve", []) if s not in result]
    failures += [f"필수 문자열 없음: {s}" for s in checks.get("must_contain", []) if s not in changed_text]
    failures += [f"금지 문자열 있음: {s}" for s in checks.get("must_not_contain", []) if s in changed_text]
    if not format_ok(checks.get("format"), changed):
        failures.append(f"형식 아님: {checks.get('format')}")
    return {"code_ok": not failures, "code_failures": failures, "changed_markdown": changed_text}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--name", default="edit", help="결과 폴더 이름(수정 전후 비교용)")
    parser.add_argument("--ids", help="쉼표로 구분한 사례 id만 실행")
    parser.add_argument("--model", default=":".join(HAIKU), help="provider:model (기본 Claude Haiku)")
    args = parser.parse_args()
    verify_gold()
    cases = json.loads((EVAL_DIR / "edit.json").read_text())
    if args.ids:
        cases = [case for case in cases if case["id"] in args.ids.split(",")]
    cases = cases[: args.limit] if args.limit else cases
    provider, model = args.model.split(":", 1)
    load_keys({"claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}[provider])
    os.environ["AI_ALLOWED_LLM_MODELS"] = "*"
    from app.modules.markdown_edit.application.generate_markdown_edit import GenerateMarkdownEditUseCase
    from app.modules.markdown_edit.domain.entities import MarkdownEditRequest, MarkdownEditTarget
    from app.modules.markdown_edit.domain.markdown_target_scope import apply_markdown_edit
    from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import build_markdown_editor

    use_case = GenerateMarkdownEditUseCase(build_markdown_editor(provider=provider, model=model))

    def run(job):
        case, run_index = job
        request = MarkdownEditRequest(
            instruction=case["instruction"], markdown=case["markdown"], target=MarkdownEditTarget(**case["target"]),
            edit_goal=case["edit_goal"], edit_operation=case["edit_operation"], edit_destination=case["edit_destination"],
            output_language=case.get("output_language"),
        )
        started = time.monotonic()
        row = {"id": case["id"], "category": case["category"], "edit_goal": case["edit_goal"], "run": run_index}
        try:
            edit = use_case.execute(request).edit
            result = apply_markdown_edit(case["markdown"], edit)
            row.update(error=None, result_markdown=result, scope_expanded=edit.scope_expanded, changed=edit.changed,
                       **check(case, case["markdown"], result))
        except Exception as exc:  # 계약 실패도 결과(실패)로 남긴다.
            row.update(error=f"{type(exc).__name__}: {str(exc)[:300]}", result_markdown=None, code_ok=False,
                       code_failures=["실행 오류"], changed_markdown="")
        row["seconds"] = round(time.monotonic() - started, 3)
        return row

    jobs = [(case, r) for case in cases for r in range(RUNS)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(run, jobs))
    out = results_dir(args.name)
    write_jsonl(out / "rows.jsonl", rows)
    print("code ok", sum(r["code_ok"] for r in rows), "/", len(rows), "errors", sum(bool(r["error"]) for r in rows))
    print("saved to", out)


if __name__ == "__main__":
    main()
