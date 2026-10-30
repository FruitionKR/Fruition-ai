"""Skill 평가. 의도 분류(LLM·Jev 켬/끔), Skill 작성, Skill 시험 실행을 운영 경로로 돌려 정답과 비교한다.

사용: PYTHONPATH=<dev>/pipeline python run_skill.py [--limit N] [--ids si-01,st-01]
- 모델: Claude Haiku. Jev 분류는 JEV_SKILL_INTENT_ENABLED와 TYPESAFE_API_KEY로 켠다.
- 시험 실행 결과의 판정 기준은 grade_features.py(Codex)가 본다. 여기서는 코드 검사만 한다.
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
from run_edit import format_ok  # noqa: E402

WORKSPACE, USER = "ws-eval", "user-eval"


class Repository:
    """권한 확인만 쓰는 Skill 저장소. 평가는 개인 Skill만 작성하고 저장하지 않는다."""

    def can_manage_team(self, workspace_id, user_id) -> bool:
        return True


class CaseReferences:
    def __init__(self, references: list[dict]) -> None:
        from app.modules.skill.domain.entities import SkillAuthoringReference

        self._by_id = {r["id"]: SkillAuthoringReference(id=r["id"], name=r["name"], markdown=r["markdown"]) for r in references}

    def read(self, *, workspace_id: str, user_id: str, document_id: str):
        return self._by_id[document_id]


def intent_got(raw: dict, has_references: bool) -> dict:
    from app.modules.skill.application.author_skill import _classified_requires_source, _intent_result

    decision, capabilities, reference_mode = _intent_result(raw, has_references)
    requires_source = _classified_requires_source(raw)
    # 코드는 문서 생성이 아닌 Skill의 원문 필요 여부를 쓰지 않으므로 그 경우 False로 맞춘다.
    if not capabilities or "document-create" not in capabilities:
        requires_source = False
    return {"decision": decision, "skill_kinds": sorted(capabilities or ()), "reference_mode": reference_mode, "requires_source": bool(requires_source)}


def comparable_intent(expected: dict) -> dict:
    return {**expected, "skill_kinds": sorted(expected["skill_kinds"])}


def authoring_failures(case: dict, result) -> list[str]:
    expected, checks, failures = case["expected"], case.get("checks") or {}, []
    if result.status != expected["status"]:
        return [f"상태 {result.status}"]
    proposal = result.proposal
    if checks.get("proposal_is_null"):
        failures += ["초안이 있으면 안 됨"] if proposal else []
    if checks.get("question_equals") and result.question != checks["question_equals"]:
        failures.append("되묻는 문장이 다름")
    issues_text = " ".join(f"{issue.category} {issue.text} {issue.reason}" for issue in result.issues)
    failures += [f"차단 사유 없음: {c}" for c in checks.get("issue_categories_include", []) if c not in issues_text]
    failures += [f"차단 설명 없음: {t}" for t in checks.get("issue_text_include", []) if t not in issues_text]
    if proposal is None:
        return failures
    text = proposal.instructions_markdown
    if sorted(proposal.capabilities) != sorted(expected["capabilities"]):
        failures.append(f"기능 {list(proposal.capabilities)}")
    if checks.get("slug_pattern") and not re.fullmatch(checks["slug_pattern"], proposal.name):
        failures.append(f"slug 형식 아님: {proposal.name}")
    if checks.get("allowed_tools") and sorted(proposal.allowed_tools) != sorted(checks["allowed_tools"]):
        failures.append("허용 도구가 다름")
    if checks.get("description_equals") and proposal.description != checks["description_equals"]:
        failures.append("설명이 바뀜")
    failures += [f"지침에 없음: {group}" for group in checks.get("instructions_must_mention", []) if not any(w in text for w in group)]
    failures += [f"지침에 있으면 안 됨: {w}" for w in checks.get("instructions_must_not_contain", []) if w in text]
    order = [text.find(word) for word in checks.get("instructions_step_order", [])]
    if order and (min(order) < 0 or order != sorted(order)):
        failures.append("지침 순서가 다름")
    if checks.get("instructions_markdown_equals") and text.strip() != checks["instructions_markdown_equals"].strip():
        failures.append("고정 양식 지침이 다름")
    if "requires_source" in checks and bool(proposal.requires_source) != checks["requires_source"]:
        failures.append("원문 필요 여부가 다름")
    return failures


def test_run_failures(case: dict, original: str, result: str) -> list[str]:
    """코드로 확실히 볼 수 있는 검사만 한다. 나머지 형식 조건은 판정 기준으로 본다."""
    checks, failures = case["checks"], []
    failures += [f"보존 문자열 없음: {s}" for s in checks.get("preserve", []) if s not in result]
    failures += [f"필수 문자열 없음: {s}" for s in checks.get("must_contain", []) if s not in result]
    failures += [f"금지 문자열 있음: {s}" for s in checks.get("must_not_contain", []) if s in result]
    lines = result.splitlines()
    for token in filter(None, (checks.get("format") or "").split(";")):
        kind, _, arg = token.partition(":")
        if kind in {"checklist", "table"} and not format_ok(kind, lines):
            failures.append(f"형식 아님: {kind}")
        if kind == "table" and arg and not arg.startswith("rows"):
            header = next((line for line in lines if line.strip().startswith("|")), "")
            failures += [f"표 머리글 없음: {h}" for h in arg.split("|") if h not in header]
        if kind == "outside_target_unchanged" and case.get("target"):
            t = case["target"]
            before = original.splitlines()
            head, tail = before[: t["start_line"] - 1], before[t["end_line"]:]
            if lines[: len(head)] != head or (tail and lines[-len(tail):] != tail):
                failures.append("대상 밖 줄이 바뀜")
        if kind in {"english", "korean"} and not format_ok(kind, lines):
            failures.append(f"언어 아님: {kind}")
    if result == original:
        failures.append("결과가 원문과 같음")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", help="쉼표로 구분한 사례 id만 실행")
    args = parser.parse_args()
    verify_gold()
    cases = json.loads((EVAL_DIR / "skill.json").read_text())
    if args.ids:
        cases = [case for case in cases if case["id"] in args.ids.split(",")]
    cases = cases[: args.limit] if args.limit else cases
    load_keys("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY")
    os.environ["AI_ALLOWED_LLM_MODELS"] = "*"
    from app.core.jev_client import JEV_SKILL_INTENT_ENABLED_ENV
    from app.modules.markdown_edit.application.generate_markdown_document import GenerateMarkdownDocumentUseCase
    from app.modules.markdown_edit.application.generate_markdown_edit import GenerateMarkdownEditUseCase
    from app.modules.markdown_edit.domain.entities import MarkdownEditTarget
    from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import build_markdown_editor
    from app.modules.skill.application.author_skill import AuthorSkillUseCase
    from app.modules.skill.application.manage_skill import ManageSkillUseCase
    from app.modules.skill.application.skill_test_run import SkillTestRunUseCase
    from app.modules.skill.domain.entities import SkillAuthoringReference
    from app.modules.skill.infrastructure.chat_completions_skill_authoring_generator import build_skill_authoring_generator
    from app.modules.skill.infrastructure.jev_skill_intent_classifier import with_jev_skill_intent

    llm = build_skill_authoring_generator(provider=HAIKU[0], model=HAIKU[1])
    os.environ[JEV_SKILL_INTENT_ENABLED_ENV] = "true"
    jev = with_jev_skill_intent(llm)
    assert jev is not llm, "Jev Skill 분류기를 만들지 못했습니다"
    editor = build_markdown_editor(provider=HAIKU[0], model=HAIKU[1])

    def run(job):
        case, variant, run_index = job
        started = time.monotonic()
        row = {"id": case["id"], "kind": case["kind"], "category": case["category"], "variant": variant, "run": run_index, "error": None}
        generator = jev if variant == "on" else llm
        try:
            if case["kind"] == "intent":
                references = tuple(SkillAuthoringReference(id=r["id"], name=r["name"], markdown=r["markdown"]) for r in case["references"])
                got = intent_got(generator.classify(case["instruction"], references, requested_description=case["requested_description"]), bool(references))
                accepted = [comparable_intent(case["expected"]), *(comparable_intent(a) for a in case.get("acceptable", []))]
                row.update(got=got, ok=got in accepted, failures=[] if got in accepted else ["분류가 다름"])
            elif case["kind"] == "authoring":
                use_case = AuthorSkillUseCase(generator, CaseReferences(case["references"]), ManageSkillUseCase(Repository()))
                result = use_case.execute(
                    workspace_id=WORKSPACE, user_id=USER, scope_type="personal", instruction=case["instruction"],
                    reference_document_ids=tuple(r["id"] for r in case["references"]),
                    allow_clarification=case.get("allow_clarification", True), description=case.get("requested_description"),
                    authoring_mode=case.get("authoring_mode", "enhance"),
                )
                failures = authoring_failures(case, result)
                row.update(got={"status": result.status, "capabilities": list(result.proposal.capabilities) if result.proposal else []},
                           proposal=result.proposal.instructions_markdown if result.proposal else None, ok=not failures, failures=failures)
            else:
                skill = case["skill"]
                use_case = SkillTestRunUseCase(
                    AuthorSkillUseCase(generator, CaseReferences([]), ManageSkillUseCase(Repository())),
                    GenerateMarkdownEditUseCase(editor), GenerateMarkdownDocumentUseCase(editor),
                )
                result = use_case.execute(
                    workspace_id=WORKSPACE, user_id=USER, name=skill["name"], description=skill["description"],
                    instructions_markdown=skill["instructions_markdown"], capabilities=tuple(skill["capabilities"]),
                    document_id=f"doc-{case['id']}", markdown=case["markdown"],
                    target=MarkdownEditTarget(**case["target"]) if case.get("target") else None,
                )
                failures = ["차단됨"] if result.status != "completed" else test_run_failures(case, case["markdown"], result.result_markdown or "")
                row.update(got={"status": result.status}, result_markdown=result.result_markdown, ok=not failures, failures=failures)
        except Exception as exc:  # 계약 위반·지원 안 함 판정도 결과로 남긴다.
            row.update(error=f"{type(exc).__name__}: {str(exc)[:300]}", ok=False, failures=["실행 오류"])
            code = getattr(exc, "code", None)
            # 지원하지 않는 요청은 작성 단계에서 예외로 끝난다. 정답이 그 판단이면 성공이다.
            if case["kind"] == "authoring" and code and case["expected"]["status"] == code:
                row.update(ok=True, failures=[])
        row["seconds"] = round(time.monotonic() - started, 3)
        return row

    # Jev 켬/끔은 분류가 달라지는 의도 분류·작성에만 의미가 있어 시험 실행은 끔으로만 돌린다.
    jobs = [(c, v, r) for c in cases for v in (("off", "on") if c["kind"] != "test_run" else ("off",)) for r in range(RUNS)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(run, jobs))
    out = results_dir("skill")
    write_jsonl(out / "rows.jsonl", rows)
    for kind in ("intent", "authoring", "test_run"):
        for variant in ("off", "on"):
            mine = [r for r in rows if r["kind"] == kind and r["variant"] == variant]
            if mine:
                print(kind, variant, sum(r["ok"] for r in mine), "/", len(mine), "errors", sum(bool(r["error"]) for r in mine))
    print("saved to", out)


if __name__ == "__main__":
    main()
