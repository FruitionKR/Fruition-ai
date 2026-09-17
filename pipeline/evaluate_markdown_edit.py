"""동일 고정 입력으로 기준 커밋과 현재 편집기의 품질·지연·호출 수를 비교한다."""

import argparse
import copy
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from app.modules.markdown_edit.domain.entities import (
    MarkdownEditRequest,
    MarkdownEditTarget,
)
from app.modules.markdown_edit.domain.markdown_output_contract import (
    MarkdownOutputContractError,
)
from app.modules.markdown_edit.infrastructure import (
    chat_completions_markdown_editor as editor_module,
)
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)
from markdown_edit_gfm_lab import (
    CASES,
    DEFAULT_PROMPT,
    DEFAULT_SOURCE_EDIT_PROMPT,
    evaluate_replacement,
)


class RecordingClient:
    def __init__(self, model, first_call=None):
        self.client = ChatCompletionsJsonClient(
            ChatClientConfig(
                api_key=os.environ["OPENAI_API_KEY"],
                provider="openai",
                model=model,
                temperature=None,
                timeout_seconds=180,
                json_mode=True,
            )
        )
        self.calls = []
        self.first_call = copy.deepcopy(first_call)

    def complete_json(self, system_prompt, user_prompt):
        if self.first_call is not None:
            call = self.first_call
            self.first_call = None
            self.calls.append({**call, "replayed": True})
            return call["response"]
        started = time.perf_counter()
        call = {
            "kind": "evaluation"
            if "proposal" in json.loads(user_prompt)
            else "generation"
        }
        self.calls.append(call)
        try:
            result = self.client.complete_json(system_prompt, user_prompt)
            call["response"] = result
            return result
        finally:
            call["seconds"] = round(time.perf_counter() - started, 3)


class WithoutEvaluator(editor_module.ChatCompletionsMarkdownEditor):
    # 평가 실험의 대조군이며 실제 서비스의 선택 옵션이 아니다.
    def _evaluate_edit(self, *args):
        return []


def load_baseline_editor(ref):
    def source(path):
        return subprocess.check_output(
            ["git", "show", f"{ref}:pipeline/{path}"], text=True
        )

    domain = types.ModuleType("baseline_markdown_contract")
    sys.modules[domain.__name__] = domain
    exec(  # noqa: S102 - 사용자가 지정한 로컬 Git 기준 커밋의 코드를 비교 실행한다.
        compile(
            source("app/modules/markdown_edit/domain/markdown_output_contract.py"),
            "baseline_contract",
            "exec",
        ),
        domain.__dict__,
    )
    namespace = {
        "__file__": editor_module.__file__,
        "__name__": "baseline_markdown_editor",
    }
    exec(  # noqa: S102 - 사용자가 지정한 로컬 Git 기준 커밋의 코드를 비교 실행한다.
        compile(
            source(
                "app/modules/markdown_edit/infrastructure/chat_completions_markdown_editor.py"
            ),
            "baseline_editor",
            "exec",
        ),
        namespace,
    )
    for name in (
        "protect_markdown",
        "repair_markdown_output",
        "validate_markdown_output",
    ):
        namespace[name] = getattr(domain, name)
    return (
        namespace["ChatCompletionsMarkdownEditor"],
        source("prompts/markdown_edit.system.md"),
        source("prompts/markdown_source_edit.system.md"),
    )


def run_case(
    editor_class,
    variant,
    case,
    run,
    model,
    prompt=None,
    source_prompt=None,
    first_call=None,
):
    client = RecordingClient(model, first_call)
    editor = editor_class(
        client,
        prompt or DEFAULT_PROMPT.read_text(),
        source_edit_system_prompt=source_prompt
        or DEFAULT_SOURCE_EDIT_PROMPT.read_text(),
    )
    request = MarkdownEditRequest(
        instruction=case.instruction,
        markdown=case.markdown,
        edit_goal=case.edit_goal,
        target=MarkdownEditTarget(
            type="whole_document",
            start_line=1,
            end_line=len(case.markdown.splitlines()),
        ),
    )
    started = time.perf_counter()
    returned = False
    replacement = ""
    contract_failures = []
    error_type = None
    try:
        replacement = editor.generate_edit(request).edit.replacement_markdown
        returned = True
    except MarkdownOutputContractError as exc:
        replacement = exc.replacement_markdown
        contract_failures = list(exc.failures)
    except (RuntimeError, ValueError) as exc:
        # 외부 오류 문자열에 인증 정보가 포함될 수 있어 예외 종류만 기록한다.
        error_type = type(exc).__name__
    external_failures = evaluate_replacement(case, replacement)
    return {
        "variant": variant,
        "id": case.id,
        "run": run,
        "returned": returned,
        "passed": returned and not external_failures,
        "seconds": round(
            time.perf_counter()
            - started
            + (first_call["seconds"] if first_call else 0),
            3,
        ),
        "contract_failures": contract_failures,
        "external_failures": external_failures,
        "error_type": error_type,
        "replacement_markdown": replacement,
        "calls": client.calls,
    }


def summarize(rows):
    return {
        "total": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "returned": sum(r["returned"] for r in rows),
        "invalid_returned": sum(
            r["returned"] and bool(r["external_failures"]) for r in rows
        ),
        "errors": sum(r["error_type"] is not None for r in rows),
        "average_seconds": round(statistics.mean(r["seconds"] for r in rows), 3),
        "median_seconds": round(statistics.median(r["seconds"] for r in rows), 3),
        "generation_calls": sum(
            c["kind"] == "generation" for r in rows for c in r["calls"]
        ),
        "evaluation_calls": sum(
            c["kind"] == "evaluation" for r in rows for c in r["calls"]
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--model", default="gpt-5-nano")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--replay",
        type=Path,
        help="이전 비교 JSON의 최초 출력을 동일하게 재생하고 평가자 제거 대조군도 비교",
    )
    args = parser.parse_args()
    if args.repeat < 1 or args.workers < 1:
        parser.error("repeat와 workers는 1 이상이어야 합니다.")
    cases = [c for c in CASES if not args.case_ids or c.id in args.case_ids]
    if not cases or set(args.case_ids or []) - {c.id for c in CASES}:
        parser.error("알 수 없는 case입니다.")
    baseline_ref = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.baseline_ref + "^{commit}"],
        text=True,
    ).strip()
    baseline, baseline_prompt, baseline_source_prompt = load_baseline_editor(
        baseline_ref
    )
    variants = [
        ("baseline", baseline, baseline_prompt, baseline_source_prompt),
        ("candidate", editor_module.ChatCompletionsMarkdownEditor, None, None),
    ]
    if args.replay:
        variants.insert(1, ("without_evaluator", WithoutEvaluator, None, None))

    def pair(case, run, draft=None):
        order = variants if run % 2 else list(reversed(variants))
        rows = [
            run_case(
                cls,
                name,
                case,
                run,
                args.model,
                prompt,
                source_prompt,
                draft["calls"][0] if draft else None,
            )
            for name, cls, prompt, source_prompt in order
        ]
        if draft:
            for row in rows:
                row["draft_id"] = f"{draft['variant']}:{case.id}:{run}"
        return rows

    payload = {
        "baseline_ref": baseline_ref,
        "model": args.model,
        "reasoning_effort": "medium",
        "repeat": args.repeat,
        "workers": args.workers,
        "method": "독립 실모델 호출, 고정 19종 입력, 동일 생성 설정·최대 2회 생성, router 제외. 외부 기존 코드 검사로 채점하며 미반환도 실패 처리.",
        "replay": bool(args.replay),
        "cases": [asdict(c) for c in cases],
        "sha256": {
            str(
                p.resolve().relative_to(Path(__file__).resolve().parent)
            ): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                Path(editor_module.__file__),
                DEFAULT_PROMPT,
                DEFAULT_SOURCE_EDIT_PROMPT,
                editor_module.DEFAULT_MARKDOWN_EDIT_EVALUATOR_PROMPT,
                Path(__file__).parent
                / "app/modules/markdown_edit/domain/markdown_output_contract.py",
            ]
        },
        "results": [],
    }
    if args.replay:
        payload["method"] = (
            "동일 최초 출력 재생 대조 실험. 이후 호출만 실모델 호출. seconds는 기록된 최초 생성 시간과 이후 실행 시간을 합산한 환산값. 미반환도 실패 처리."
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        if args.replay:
            drafts = json.loads(args.replay.read_text())["results"]
            case_by_id = {c.id: c for c in cases}
            futures = [
                pool.submit(pair, case_by_id[r["id"]], r["run"], r)
                for r in drafts
                if r["id"] in case_by_id
            ]
        else:
            futures = [
                pool.submit(pair, case, run)
                for case in cases
                for run in range(1, args.repeat + 1)
            ]
        for future in as_completed(futures):
            rows = future.result()
            payload["results"].extend(rows)
            args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            print(
                json.dumps(
                    [
                        {
                            k: r[k]
                            for k in [
                                "variant",
                                "id",
                                "run",
                                "passed",
                                "seconds",
                                "error_type",
                            ]
                        }
                        for r in rows
                    ],
                    ensure_ascii=False,
                ),
                flush=True,
            )
    payload["summary"] = {
        name: summarize([r for r in payload["results"] if r["variant"] == name])
        for name, *_ in variants
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
