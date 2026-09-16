#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.core.llm_env import SUPPORTED_LLM_PROVIDERS
from app.modules.agent.domain.entities import (
    ActiveMarkdownContext,
    AgentConversationContext,
    AgentTurnRequest,
)
from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    build_agent_turn_router,
)
from app.modules.markdown_edit.domain.entities import MarkdownEditTarget
from app.modules.query.domain.entities import ConversationMessage


DEFAULT_DATASET = Path(__file__).with_name("evals") / "agent_turn_router.jsonl"
ROUTE_FIELDS = (
    "action",
    "retrieval_source",
    "document_operation",
    "persist",
    "required_capabilities",
    "edit_goal",
    "edit_operation",
    "edit_destination",
)


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    case_ids = [case["id"] for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("라우팅 평가 case id는 중복될 수 없습니다.")
    for case in cases:
        expected = case.get("expected")
        acceptable = case.get("acceptable", [])
        if not isinstance(expected, dict):
            raise ValueError(f"{case['id']}: expected는 route JSON object여야 합니다.")
        if not isinstance(acceptable, list) or not all(
            isinstance(route, dict) for route in acceptable
        ):
            raise ValueError(f"{case['id']}: acceptable은 route JSON object 배열이어야 합니다.")
        routes = [expected, *acceptable]
        for index, route in enumerate(routes):
            missing = set(ROUTE_FIELDS) - route.keys()
            if missing:
                label = "expected" if index == 0 else f"acceptable[{index - 1}]"
                raise ValueError(f"{case['id']}: {label} 필드 누락: {sorted(missing)}")
        for route in routes[1:]:
            if (
                route["document_operation"] != routes[0]["document_operation"]
                or route["persist"] != routes[0]["persist"]
            ):
                raise ValueError(
                    f"{case['id']}: acceptable route는 문서 변경·영속성 경계를 바꿀 수 없습니다."
                )
    return cases


def request_from_case(case: dict[str, Any]) -> AgentTurnRequest:
    context = case.get("context", {})
    active_markdown = context.get("active_markdown")
    target = active_markdown.get("target") if active_markdown else None
    recent_messages = tuple(
        ConversationMessage(**message)
        for message in context.get("recent_messages", [])
    )
    return AgentTurnRequest(
        message=case["message"],
        document_id="evaluation-document" if active_markdown else None,
        active_markdown_context=(
            ActiveMarkdownContext(
                markdown=active_markdown["markdown"],
                target=MarkdownEditTarget(**target) if target else None,
            )
            if active_markdown
            else None
        ),
        conversation_context=(
            AgentConversationContext(recent_messages=recent_messages)
            if recent_messages
            else None
        ),
        allow_web_search=context.get("allow_web_search"),
    )


def route_values(route: object) -> dict[str, object]:
    values = asdict(route)  # type: ignore[arg-type]
    values["required_capabilities"] = sorted(values["required_capabilities"])
    return {field: values[field] for field in ROUTE_FIELDS}


def expected_route_values(case: dict[str, Any]) -> list[dict[str, object]]:
    routes: list[dict[str, object]] = []
    for raw_route in [case["expected"], *case.get("acceptable", [])]:
        route = dict(raw_route)
        route["required_capabilities"] = sorted(route["required_capabilities"])
        routes.append(route)
    return routes


def evaluate_cases(router: object, cases: list[dict[str, Any]]) -> dict[str, object]:
    results: list[dict[str, object]] = []
    confusion: Counter[str] = Counter()
    route_correct = 0
    agent_turn_failed = 0
    mutation_false_positives = 0

    for case in cases:
        accepted = expected_route_values(case)
        expected = accepted[0]
        try:
            actual = route_values(router.route(request_from_case(case)))  # type: ignore[attr-defined]
        except Exception as exc:
            agent_turn_failed += 1
            failure = {
                "id": case["id"],
                "route_correct": False,
                "error_code": type(exc).__name__,
            }
            if isinstance(exc, AgentTurnRouteContractError):
                failure["contract_failures"] = exc.failures
            results.append(failure)
            continue

        is_correct = actual in accepted
        route_correct += int(is_correct)
        mutation_false_positives += int(
            expected["document_operation"] == "none"
            and actual["document_operation"] != "none"
        )
        confusion[f"{expected['action']} -> {actual['action']}"] += 1
        result = {
            "id": case["id"],
            "route_correct": is_correct,
            "expected": expected,
            "actual": actual,
        }
        if len(accepted) > 1:
            result["acceptable"] = accepted[1:]
        results.append(result)

    return {
        "summary": {
            "total": len(cases),
            "route_correct": route_correct,
            "agent_turn_failed": agent_turn_failed,
            "mutation_false_positives": mutation_false_positives,
            "confusion": dict(sorted(confusion.items())),
        },
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent turn router seed 평가")
    parser.add_argument("--provider", choices=SUPPORTED_LLM_PROVIDERS, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate_cases(
        build_agent_turn_router(provider=args.provider, model=args.model),
        load_cases(args.dataset),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = report["summary"]
    assert isinstance(summary, dict)
    return int(
        summary["route_correct"] != summary["total"]
        or summary["agent_turn_failed"] != 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
