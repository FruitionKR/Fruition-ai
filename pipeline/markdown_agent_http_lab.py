from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import api
from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    DEFAULT_AGENT_TURN_ROUTER_PROMPT,
    ChatCompletionsTurnRouter,
)
from app.modules.agent.interfaces.http.dependencies import get_handle_agent_turn_use_case
from app.modules.markdown_edit.application.generate_markdown_document import GenerateMarkdownDocumentUseCase
from app.modules.markdown_edit.application.generate_markdown_edit import GenerateMarkdownEditUseCase
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import (
    DEFAULT_MARKDOWN_EDIT_PROMPT,
    DEFAULT_MARKDOWN_SOURCE_EDIT_PROMPT,
    ChatCompletionsMarkdownEditor,
)
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
)


class _UnexpectedQueryUseCase:
    def execute(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Markdown E2E 시나리오가 query 경로로 잘못 라우팅되었습니다.")


DEFAULT_AGENT_PROVIDER = "openai"


def _build_use_case(args: argparse.Namespace) -> HandleAgentTurnUseCase:
    router_client = ChatCompletionsJsonClient(
        ChatClientConfig(
            api_key=args.api_key,
            model=args.model,
            provider=DEFAULT_AGENT_PROVIDER,
            temperature=0.0,
            timeout_seconds=args.timeout_seconds,
            json_mode=True,
        )
    )
    editor_client = ChatCompletionsJsonClient(
        ChatClientConfig(
            api_key=args.api_key,
            model=args.model,
            provider=DEFAULT_AGENT_PROVIDER,
            temperature=0.2,
            timeout_seconds=args.timeout_seconds,
            json_mode=True,
        )
    )
    editor = ChatCompletionsMarkdownEditor(
        editor_client,
        Path(args.prompt).read_text(encoding="utf-8"),
        source_edit_system_prompt=Path(args.source_edit_prompt).read_text(encoding="utf-8"),
        context_lines=args.context_lines,
    )
    return HandleAgentTurnUseCase(
        router=ChatCompletionsTurnRouter(
            router_client,
            Path(args.router_prompt).read_text(encoding="utf-8"),
        ),
        query_use_case=_UnexpectedQueryUseCase(),  # type: ignore[arg-type]
        markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
        markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
    )


def _selection_cleanup(client: TestClient, model: str) -> dict[str, Any]:
    markdown = "# 배포 안내\n\n배포를 하기 전에 테스트를 한다.\n\n문제가 없으면 승인한다."
    response = client.post(
        "/agent/turn",
        json={
            "message": "선택한 문장만 자연스럽고 간결하게 다듬어줘.",
            "provider": DEFAULT_AGENT_PROVIDER,
            "model": model,
            "active_markdown_context": {
                "markdown": markdown,
                "target": {"type": "selection", "start_line": 3, "end_line": 3},
            },
        },
    )
    body = response.json()
    replacement = body.get("edit", {}).get("replacement_markdown", "") if response.status_code == 200 else ""
    failures: list[str] = []
    if response.status_code != 200:
        failures.append(f"HTTP {response.status_code}: {body}")
    elif body.get("action") != "markdown_edit":
        failures.append(f"action 불일치: {body.get('action')}")
    else:
        if "# 배포 안내" in replacement or "문제가 없으면" in replacement:
            failures.append("읽기 전용 문맥이 replacement에 포함됨")
        if "배포" not in replacement or "테스트" not in replacement:
            failures.append("선택 문장의 핵심 정보가 누락됨")
    return {"id": "selection_cleanup", "passed": not failures, "failures": failures, "response": body}


def _structured_translation(client: TestClient, model: str) -> dict[str, Any]:
    markdown = "# Deploy guide\n\nRead the [install guide](https://example.com/install).\n\n```bash\n./deploy.sh --prod\n```"
    response = client.post(
        "/agent/turn",
        json={
            "message": "보이는 영어 문장을 한국어로 번역해줘.",
            "provider": DEFAULT_AGENT_PROVIDER,
            "model": model,
            "active_markdown_context": {
                "markdown": markdown,
                "target": {"type": "whole_document", "start_line": 1, "end_line": 7},
            },
        },
    )
    body = response.json()
    replacement = body.get("edit", {}).get("replacement_markdown", "") if response.status_code == 200 else ""
    failures: list[str] = []
    if response.status_code != 200:
        failures.append(f"HTTP {response.status_code}: {body}")
    elif body.get("action") != "markdown_edit":
        failures.append(f"action 불일치: {body.get('action')}")
    else:
        for literal in ("https://example.com/install", "```bash\n./deploy.sh --prod\n```"):
            if literal not in replacement:
                failures.append(f"보존 대상 누락: {literal}")
    return {"id": "structured_translation", "passed": not failures, "failures": failures, "response": body}


def _partial_fence_rejection(client: TestClient, model: str) -> dict[str, Any]:
    markdown = "# 실행\n\n```bash\n./deploy.sh\n```"
    response = client.post(
        "/agent/turn",
        json={
            "message": "선택한 코드 문장을 자연스럽게 다듬어줘.",
            "provider": DEFAULT_AGENT_PROVIDER,
            "model": model,
            "active_markdown_context": {
                "markdown": markdown,
                "target": {"type": "selection", "start_line": 4, "end_line": 4},
            },
        },
    )
    body = response.json()
    detail = body.get("detail", {})
    failures = []
    if response.status_code != 422:
        failures.append(f"예상 HTTP 422, 실제 {response.status_code}: {body}")
    elif detail.get("code") != "markdown_target_crosses_structure":
        failures.append(f"오류 코드 불일치: {detail.get('code')}")
    return {"id": "partial_fence_rejection", "passed": not failures, "failures": failures, "response": body}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="실제 /agent/turn + OpenAI Markdown 편집 E2E")
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    parser.add_argument("--model", default="gpt-5-nano")
    parser.add_argument("--prompt", default=str(DEFAULT_MARKDOWN_EDIT_PROMPT))
    parser.add_argument("--source-edit-prompt", default=str(DEFAULT_MARKDOWN_SOURCE_EDIT_PROMPT))
    parser.add_argument("--router-prompt", default=str(DEFAULT_AGENT_TURN_ROUTER_PROMPT))
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--context-lines", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    use_case = _build_use_case(args)
    api.app.dependency_overrides[get_handle_agent_turn_use_case] = lambda: use_case
    started_at = time.perf_counter()
    try:
        with TestClient(api.app) as client:
            results = [
                _selection_cleanup(client, args.model),
                _structured_translation(client, args.model),
                _partial_fence_rejection(client, args.model),
            ]
    finally:
        api.app.dependency_overrides.pop(get_handle_agent_turn_use_case, None)

    output = {
        "model": args.model,
        "passed": sum(result["passed"] for result in results),
        "total": len(results),
        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if output["passed"] != output["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
