#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.modules.agent.domain.entities import ActiveMarkdownContext, AgentTurnRequest
from app.modules.agent.infrastructure.chat_completions_turn_router import ChatCompletionsTurnRouter
from app.modules.markdown_edit.domain.entities import MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.domain.markdown_output_contract import MarkdownOutputContractError
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import ChatCompletionsMarkdownEditor
from app.modules.wiki_generation.infrastructure.chat_completions_llm import ChatClientConfig, ChatCompletionsJsonClient


DEFAULT_PROMPT = Path(__file__).parent / "prompts" / "markdown_edit.system.md"
DEFAULT_SOURCE_EDIT_PROMPT = Path(__file__).parent / "prompts" / "markdown_source_edit.system.md"
DEFAULT_ROUTER_PROMPT = Path(__file__).parent / "prompts" / "agent_turn_router.system.md"


@dataclass(frozen=True)
class MarkdownEvaluationCase:
    id: str
    instruction: str
    edit_goal: str
    markdown: str


@dataclass(frozen=True)
class MarkdownEvaluationResult:
    id: str
    run: int
    passed: bool
    elapsed_seconds: float
    failures: list[str]
    replacement_markdown: str
    route_action: str | None = None
    route_edit_goal: str | None = None


CASES = (
    MarkdownEvaluationCase(
        id="emphasis_quote",
        instruction="핵심 문장은 굵게 표시하고, 주의 문장은 인용문으로 정리해줘.",
        edit_goal="convert_format",
        markdown="핵심: 배포 전 테스트를 완료한다.\n주의: 운영 DB를 직접 수정하지 않는다.",
    ),
    MarkdownEvaluationCase(
        id="nested_list",
        instruction="상하위 관계가 드러나는 중첩 bullet 목록으로 바꿔줘.",
        edit_goal="bullet_list",
        markdown=(
            "프로젝트 설정에는 환경 변수와 로그 설정이 있다. "
            "환경 변수에는 API URL과 timeout이 있다. "
            "로그 설정에는 level과 output이 있다."
        ),
    ),
    MarkdownEvaluationCase(
        id="preserve_code_link",
        instruction="설명 문장만 자연스럽게 다듬고 code block과 링크는 그대로 유지해줘.",
        edit_goal="cleanup",
        markdown=(
            "설치를 하기 위해 아래 명령을 실행하면 된다.\n\n"
            "```bash\nnpm install\nnpm run dev\n```\n\n"
            "자세한 내용은 [설치 가이드](https://example.com/install)를 본다."
        ),
    ),
    MarkdownEvaluationCase(
        id="footnote",
        instruction="문장을 자연스럽게 다듬되 각주 reference와 definition을 유지해줘.",
        edit_goal="cleanup",
        markdown=(
            "배포 전 smoke test가 필요하다.[^1]\n\n"
            "[^1]: 운영 환경의 핵심 경로만 확인한다."
        ),
    ),
    MarkdownEvaluationCase(
        id="table",
        instruction="담당자와 상태를 Markdown 표로 바꿔줘.",
        edit_goal="convert_format",
        markdown="API 문서 담당자는 민수이고 상태는 진행 중이다. 배포 점검 담당자는 지수이고 상태는 완료다.",
    ),
    MarkdownEvaluationCase(
        id="task_list",
        instruction="작업 내용을 Markdown checklist로 바꿔줘.",
        edit_goal="checklist",
        markdown="API 문서를 검토한다. 배포 전에 smoke test를 실행한다.",
    ),
    MarkdownEvaluationCase(
        id="heading_inline_styles",
        instruction="제목을 추가하고 회귀 테스트는 굵게, 문서 미리보기는 기울임, 수동 배포는 취소선으로 표시해줘.",
        edit_goal="convert_format",
        markdown="릴리스 정책. 중요 항목은 회귀 테스트다. 선택 항목은 문서 미리보기다. 폐기 항목은 수동 배포다.",
    ),
    MarkdownEvaluationCase(
        id="numbered_list",
        instruction="작업 순서가 드러나는 Markdown 번호 목록으로 바꿔줘.",
        edit_goal="convert_format",
        markdown="첫째 의존성을 설치한다. 둘째 테스트를 실행한다. 셋째 결과를 검토한다.",
    ),
    MarkdownEvaluationCase(
        id="inline_code",
        instruction="설정 키와 값만 inline code로 표시해줘.",
        edit_goal="convert_format",
        markdown="설정 키는 DATABASE_URL이고 기본 port 값은 3000이다.",
    ),
    MarkdownEvaluationCase(
        id="preserve_image_divider",
        instruction="설명만 자연스럽게 다듬고 이미지와 구분선은 그대로 유지해줘.",
        edit_goal="cleanup",
        markdown=(
            "아키텍처 그림은 아래와 같이 확인을 할 수 있다.\n\n"
            "![아키텍처](https://example.com/architecture.png)\n\n"
            "---\n\n"
            "다음 절에서는 배포를 설명한다."
        ),
    ),
    MarkdownEvaluationCase(
        id="frontmatter",
        instruction="본문 문장만 자연스럽게 다듬고 frontmatter는 그대로 유지해줘.",
        edit_goal="cleanup",
        markdown="---\ntitle: 배포 가이드\nstatus: draft\n---\n\n배포를 하기 전에 테스트를 해야 한다.",
    ),
    MarkdownEvaluationCase(
        id="meeting_notes",
        instruction="원문에 있는 내용만 사용해 회의록으로 정리해줘.",
        edit_goal="convert_format",
        markdown="캐시 정책을 논의했다. TTL은 10분으로 결정했다. 지수는 금요일까지 부하 테스트를 진행한다.",
    ),
    MarkdownEvaluationCase(
        id="translate",
        instruction="영어로 번역해줘.",
        edit_goal="translate",
        markdown="배포 전에 운영 데이터베이스를 백업한다.",
    ),
    MarkdownEvaluationCase(
        id="shorten",
        instruction="중복을 제거하고 한 문장으로 짧게 줄여줘.",
        edit_goal="shorten",
        markdown=(
            "신규 사용자는 시작 방법을 찾기 어렵다. 시작 안내가 여러 곳에 반복되어 있다. "
            "문서마다 같은 용어를 다르게 사용하므로 신규 사용자가 이해하기 어렵다. "
            "따라서 시작 안내를 합치고 용어를 통일해야 한다."
        ),
    ),
    MarkdownEvaluationCase(
        id="translate_structured",
        instruction="보이는 문장은 한국어로 번역하고 Markdown 구조, URL, code는 유지해줘.",
        edit_goal="translate",
        markdown=(
            "# Deploy guide\n\n"
            "Read the [install guide](https://example.com/install).[^1]\n\n"
            "```bash\nnpm install\n```\n\n"
            "| Environment | Status |\n| --- | --- |\n| Production | Ready |\n\n"
            "[^1]: Official documentation"
        ),
    ),
    MarkdownEvaluationCase(
        id="shorten_anchors",
        instruction="핵심 literal을 유지하면서 한 문장으로 짧게 줄여줘.",
        edit_goal="shorten",
        markdown="API cache의 TTL은 10분이다. 반복된 설명을 제거하되 운영자는 이 값을 확인해야 한다.",
    ),
    MarkdownEvaluationCase(
        id="math",
        instruction="관계식을 display math로 표시해줘.",
        edit_goal="convert_format",
        markdown="에너지와 질량의 관계식은 E = mc^2이다.",
    ),
    MarkdownEvaluationCase(
        id="mermaid",
        instruction="요청, 검토, 적용 순서를 Mermaid flowchart로 바꿔줘.",
        edit_goal="convert_format",
        markdown="요청을 받은 다음 검토하고, 검토가 끝나면 적용한다.",
    ),
    MarkdownEvaluationCase(
        id="mixed_preservation",
        instruction="문장만 자연스럽게 다듬고 기존 Markdown 구조와 값은 보존해줘.",
        edit_goal="cleanup",
        markdown=(
            "---\ntitle: 운영 점검\n---\n\n"
            "# 배포\n\n[Runbook](https://example.com/runbook)을 확인을 한다.[^1]\n\n"
            "```bash\n./deploy.sh\n```\n\n"
            "| 환경 | 상태 |\n| --- | --- |\n| prod | ready |\n\n"
            "[^1]: 승인 후 실행한다."
        ),
    ),
)


def evaluate_replacement(case: MarkdownEvaluationCase, replacement: str) -> list[str]:
    checks = {
        "emphasis_quote": _check_emphasis_quote,
        "nested_list": _check_nested_list,
        "preserve_code_link": _check_preserve_code_link,
        "footnote": _check_footnote,
        "table": _check_table,
        "task_list": _check_task_list,
        "heading_inline_styles": _check_heading_inline_styles,
        "numbered_list": _check_numbered_list,
        "inline_code": _check_inline_code,
        "preserve_image_divider": _check_preserve_image_divider,
        "frontmatter": _check_frontmatter,
        "meeting_notes": _check_meeting_notes,
        "translate": _check_translate,
        "shorten": _check_shorten,
        "translate_structured": _check_translate_structured,
        "shorten_anchors": _check_shorten_anchors,
        "math": _check_math,
        "mermaid": _check_mermaid,
        "mixed_preservation": _check_mixed_preservation,
    }
    return checks[case.id](replacement)


def accepted_route_goals(case: MarkdownEvaluationCase) -> tuple[str, ...]:
    accepted = {
        "emphasis_quote": ("convert_format", "style_change"),
        "nested_list": ("bullet_list",),
        "preserve_code_link": ("cleanup", "style_change"),
        "footnote": ("cleanup", "style_change"),
        "table": ("convert_format",),
        "task_list": ("checklist",),
        "heading_inline_styles": ("convert_format", "style_change"),
        "numbered_list": ("convert_format",),
        "inline_code": ("convert_format",),
        "preserve_image_divider": ("cleanup", "style_change"),
        "frontmatter": ("cleanup", "style_change"),
        "meeting_notes": ("convert_format",),
        "translate": ("translate",),
        "shorten": ("shorten",),
        "translate_structured": ("translate",),
        "shorten_anchors": ("shorten",),
        "math": ("convert_format",),
        "mermaid": ("convert_format",),
        "mixed_preservation": ("cleanup", "style_change"),
    }
    return accepted[case.id]


def _check_emphasis_quote(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("배포 전 테스트", "운영 DB", "직접 수정"))
    if not re.search(r"\*\*[^*]+\*\*", markdown):
        failures.append("굵게 표시된 문장이 없음")
    if not any(line.lstrip().startswith("> ") for line in markdown.splitlines()):
        failures.append("인용문이 없음")
    return failures


def _check_nested_list(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("환경 변수", "API URL", "timeout", "로그 설정", "level", "output"))
    if not any(re.match(r"^\s{2,}[-*+]\s+", line) for line in markdown.splitlines()):
        failures.append("들여쓰기된 하위 bullet이 없음")
    if any(re.match(r"^\s*[-*+]\s+\[[ xX]\]\s+", line) for line in markdown.splitlines()):
        failures.append("일반 bullet 요청에 task list를 사용함")
    return failures


def _check_preserve_code_link(markdown: str) -> list[str]:
    failures: list[str] = []
    if "```bash\nnpm install\nnpm run dev\n```" not in markdown:
        failures.append("원본 bash code fence가 보존되지 않음")
    if "[설치 가이드](https://example.com/install)" not in markdown:
        failures.append("원본 링크가 보존되지 않음")
    return failures


def _check_footnote(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("운영 환경", "핵심 경로"))
    if "smoke test" not in markdown and "스모크 테스트" not in markdown:
        failures.append("원문 정보 누락: smoke test")
    if "[^1]" not in markdown:
        failures.append("각주 reference가 보존되지 않음")
    if not re.search(r"(?m)^\[\^1\]:\s+", markdown):
        failures.append("각주 definition이 보존되지 않음")
    if re.match(r"^\s*[-*+]\s+", markdown):
        failures.append("cleanup 요청에 list marker를 추가함")
    return failures


def _check_table(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("민수", "진행 중", "지수", "완료"))
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if len(lines) < 4 or not all(line.startswith("|") and line.endswith("|") for line in lines):
        failures.append("GFM 표 형태가 아님")
    elif not re.fullmatch(r"\|(?:\s*:?-+:?\s*\|)+", lines[1]):
        failures.append("GFM 표 구분 행이 올바르지 않음")
    return failures


def _check_task_list(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("API 문서", "smoke test"))
    nonempty_lines = [line for line in markdown.splitlines() if line.strip()]
    if len(nonempty_lines) != 2 or not all(re.match(r"^- \[ \] ", line) for line in nonempty_lines):
        failures.append("모든 작업이 미완료 task list 항목이 아님")
    return failures


def _check_heading_inline_styles(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("릴리스 정책", "회귀 테스트", "문서 미리보기", "수동 배포"))
    if not re.search(r"(?m)^#{1,6}\s+", markdown):
        failures.append("제목이 없음")
    if not re.search(r"\*\*[^*]*회귀 테스트[^*]*\*\*", markdown):
        failures.append("중요 항목이 굵게 표시되지 않음")
    if not re.search(r"(?<!\*)\*[^*]*문서 미리보기[^*]*\*(?!\*)", markdown):
        failures.append("선택 항목이 기울임으로 표시되지 않음")
    if not re.search(r"~~[^~]*수동 배포[^~]*~~", markdown):
        failures.append("폐기 항목이 취소선으로 표시되지 않음")
    return failures


def _check_numbered_list(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("의존성", "테스트", "결과", "검토"))
    numbered_lines = [line for line in markdown.splitlines() if re.match(r"^\d+\.\s+", line)]
    if len(numbered_lines) != 3:
        failures.append("세 단계 번호 목록이 아님")
    return failures


def _check_inline_code(markdown: str) -> list[str]:
    failures: list[str] = []
    if "`DATABASE_URL`" not in markdown:
        failures.append("설정 키가 inline code가 아님")
    if "`3000`" not in markdown:
        failures.append("설정 값이 inline code가 아님")
    if "```" in markdown:
        failures.append("inline code 요청에 code fence를 사용함")
    return failures


def _check_preserve_image_divider(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("배포",))
    if "![아키텍처](https://example.com/architecture.png)" not in markdown:
        failures.append("원본 이미지 Markdown이 보존되지 않음")
    if not re.search(r"(?m)^---$", markdown):
        failures.append("구분선이 보존되지 않음")
    return failures


def _check_frontmatter(markdown: str) -> list[str]:
    expected = "---\ntitle: 배포 가이드\nstatus: draft\n---"
    failures = [] if markdown.startswith(expected) else ["frontmatter가 정확히 보존되지 않음"]
    return failures + _missing_facts(markdown, ("배포", "테스트"))


def _check_meeting_notes(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("캐시 정책", "TTL", "10분", "지수", "금요일", "부하 테스트"))
    if "## 논의 사항" not in markdown or "## 결정 사항" not in markdown or "## 다음 작업" not in markdown:
        failures.append("회의록 필수 섹션이 없음")
    if re.search(r"(?m)^\s*[-*+]\s+\[[ xX]\]\s+", markdown):
        failures.append("회의록에 checkbox를 사용함")
    return failures


def _check_translate(markdown: str) -> list[str]:
    lowered = markdown.lower()
    failures: list[str] = []
    if "deploy" not in lowered and "deployment" not in lowered:
        failures.append("배포 의미가 번역되지 않음")
    if "database" not in lowered or ("backup" not in lowered and "back up" not in lowered):
        failures.append("데이터베이스 백업 의미가 번역되지 않음")
    return failures


def _check_shorten(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("신규 사용자", "시작 안내", "용어"))
    if len([line for line in markdown.splitlines() if line.strip()]) != 1:
        failures.append("한 문장 출력이 아님")
    if len(markdown) >= 130:
        failures.append("충분히 축약되지 않음")
    if re.match(r"^\s*(?:[-*+]\s+|\d+\.\s+)", markdown):
        failures.append("축약 요청에 list marker를 추가함")
    return failures


def _check_translate_structured(markdown: str) -> list[str]:
    failures: list[str] = []
    if not re.search(r"(?m)^#\s+.+", markdown):
        failures.append("번역 후 heading 구조가 없음")
    if "https://example.com/install" not in markdown:
        failures.append("번역 중 link URL이 변경됨")
    if "```bash\nnpm install\n```" not in markdown:
        failures.append("번역 중 code fence가 변경됨")
    if "[^1]" not in markdown or not re.search(r"(?m)^\[\^1\]:\s+", markdown):
        failures.append("번역 중 footnote 구조가 변경됨")
    if not re.search(r"(?m)^\|.*\|$", markdown) or "| --- | --- |" not in markdown:
        failures.append("번역 중 table 구조가 변경됨")
    visible_english = ("Deploy guide", "Read the", "install guide", "Environment", "Status", "Official documentation")
    if any(text in markdown for text in visible_english):
        failures.append("보이는 영문 text가 번역되지 않음")
    return failures


def _check_shorten_anchors(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("API", "TTL", "10분"))
    if len([line for line in markdown.splitlines() if line.strip()]) != 1:
        failures.append("anchor 축약 결과가 한 문장이 아님")
    if len(markdown) >= 53:
        failures.append("anchor 축약 결과가 원문보다 짧지 않음")
    if re.match(r"^\s*(?:[-*+]\s+|\d+\.\s+)", markdown):
        failures.append("anchor 축약에 list marker를 추가함")
    return failures


def _check_math(markdown: str) -> list[str]:
    compact = re.sub(r"\s+", "", markdown)
    failures: list[str] = []
    if not re.search(r"\$\$[\s\S]+\$\$", markdown):
        failures.append("display math 구문이 없음")
    if "E=mc^2" not in compact and "E=mc^{2}" not in compact:
        failures.append("원본 관계식이 보존되지 않음")
    if "```" in markdown:
        failures.append("수식을 code fence 안에 넣음")
    return failures


def _check_mermaid(markdown: str) -> list[str]:
    failures = _missing_facts(markdown, ("요청", "검토", "적용"))
    if not re.search(r"```mermaid\s+[\s\S]+```", markdown):
        failures.append("Mermaid code fence가 없음")
    if "flowchart" not in markdown or "-->" not in markdown:
        failures.append("Mermaid flowchart 문법이 없음")
    if "{" in markdown or "}" in markdown:
        failures.append("단순 선형 흐름에 불필요한 조건 분기를 추가함")
    return failures


def _check_mixed_preservation(markdown: str) -> list[str]:
    failures: list[str] = []
    required_fragments = (
        "---\ntitle: 운영 점검\n---",
        "# 배포",
        "[Runbook](https://example.com/runbook)",
        "[^1]",
        "```bash\n./deploy.sh\n```",
        "| 환경 | 상태 |",
        "| prod | ready |",
        "[^1]: 승인 후 실행한다.",
    )
    for fragment in required_fragments:
        if fragment not in markdown:
            failures.append(f"혼합 Markdown 구조 누락: {fragment}")
    return failures


def _missing_facts(markdown: str, facts: tuple[str, ...]) -> list[str]:
    return [f"원문 정보 누락: {fact}" for fact in facts if fact not in markdown]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenAI Markdown/GFM 편집 계약 평가")
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    parser.add_argument("--model", default="gpt-5-nano")
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument("--source-edit-prompt", default=str(DEFAULT_SOURCE_EDIT_PROMPT))
    parser.add_argument("--router-prompt", default=str(DEFAULT_ROUTER_PROMPT))
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--with-router", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--failures-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    prompt = Path(args.prompt).read_text(encoding="utf-8")
    source_edit_prompt = Path(args.source_edit_prompt).read_text(encoding="utf-8")
    client = ChatCompletionsJsonClient(
        ChatClientConfig(
            api_key=args.api_key,
            model=args.model,
            provider="openai",
            temperature=0.2,
            timeout_seconds=args.timeout_seconds,
            json_mode=True,
        )
    )
    editor = ChatCompletionsMarkdownEditor(client, prompt, source_edit_system_prompt=source_edit_prompt)
    router = None
    router_prompt = ""
    if args.with_router:
        router_prompt = Path(args.router_prompt).read_text(encoding="utf-8")
        router = ChatCompletionsTurnRouter(
            client,
            router_prompt,
        )
    selected_cases = [case for case in CASES if not args.case_ids or case.id in args.case_ids]
    unknown_case_ids = set(args.case_ids or ()) - {case.id for case in CASES}
    if unknown_case_ids:
        raise SystemExit(f"Unknown case: {', '.join(sorted(unknown_case_ids))}")
    if args.repeat < 1:
        raise SystemExit("--repeat must be greater than 0")

    results: list[MarkdownEvaluationResult] = []
    for case in selected_cases:
        for run in range(1, args.repeat + 1):
            started_at = time.perf_counter()
            target = MarkdownEditTarget(
                type="whole_document",
                start_line=1,
                end_line=max(1, len(case.markdown.splitlines())),
            )
            route = None
            edit_goal = case.edit_goal
            if router is not None:
                route = router.route(
                    AgentTurnRequest(
                        message=case.instruction,
                        active_markdown_context=ActiveMarkdownContext(markdown=case.markdown, target=target),
                    )
                )
                edit_goal = route.edit_goal or "other"
            request = MarkdownEditRequest(
                instruction=case.instruction,
                markdown=case.markdown,
                target=target,
                edit_goal=edit_goal,
                edit_operation=route.edit_operation if route and route.edit_operation else "replace",
                edit_destination=(
                    route.edit_destination if route and route.edit_destination else "target"
                ),
            )
            contract_failures: list[str] = []
            try:
                result = editor.generate_edit(request)
                replacement = result.edit.replacement_markdown
            except MarkdownOutputContractError as error:
                replacement = error.replacement_markdown
                contract_failures = list(error.failures)
            elapsed_seconds = time.perf_counter() - started_at
            failures = contract_failures + evaluate_replacement(case, replacement)
            failures = list(dict.fromkeys(failures))
            if route is not None and route.action != "markdown_edit":
                failures.insert(0, f"router action 불일치: {route.action}")
            if route is not None and route.edit_goal not in accepted_route_goals(case):
                accepted_goals = ", ".join(accepted_route_goals(case))
                failures.insert(0, f"router edit_goal 불일치: {route.edit_goal} not in ({accepted_goals})")
            results.append(
                MarkdownEvaluationResult(
                    id=case.id,
                    run=run,
                    passed=not failures,
                    elapsed_seconds=round(elapsed_seconds, 2),
                    failures=failures,
                    replacement_markdown=replacement,
                    route_action=route.action if route else None,
                    route_edit_goal=route.edit_goal if route else None,
                )
            )

    case_summary = []
    for case in selected_cases:
        case_results = [result for result in results if result.id == case.id]
        case_summary.append(
            {
                "id": case.id,
                "passed": sum(result.passed for result in case_results),
                "total": len(case_results),
                "average_seconds": round(statistics.mean(result.elapsed_seconds for result in case_results), 2),
                "max_seconds": max(result.elapsed_seconds for result in case_results),
            }
        )

    payload = {
        "model": args.model,
        "prompt": str(Path(args.prompt)),
        "prompt_metrics": {
            "editor_chars": len(prompt),
            "editor_utf8_bytes": len(prompt.encode("utf-8")),
            "source_editor_chars": len(source_edit_prompt),
            "source_editor_utf8_bytes": len(source_edit_prompt.encode("utf-8")),
            "router_chars": len(router_prompt),
            "router_utf8_bytes": len(router_prompt.encode("utf-8")),
        },
        "passed": sum(result.passed for result in results),
        "total": len(results),
        "case_summary": case_summary,
        "results": [
            asdict(result)
            for result in results
            if not args.failures_only or not result.passed
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
