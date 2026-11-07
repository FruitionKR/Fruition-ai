from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

from app.core.llm_env import (
    SUPPORTED_LLM_PROVIDERS,
    resolve_llm_provider_defaults,
    resolve_llm_selection,
)
from app.modules.agent.domain.entities import ActiveMarkdownContext, AgentTurnRequest
from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
from app.modules.agent.infrastructure.chat_completions_conversation_replier import (
    DEFAULT_CONVERSATION_REPLY_PROMPT,
    ChatCompletionsConversationReplier,
)
from app.modules.agent.infrastructure.chat_completions_turn_router import (
    DEFAULT_AGENT_TURN_ROUTER_PROMPT,
    ChatCompletionsTurnRouter,
)
from app.modules.markdown_edit.domain.entities import (
    MarkdownCreateRequest,
    MarkdownEditRequest,
    MarkdownEditTarget,
)
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import (
    DEFAULT_MARKDOWN_CREATE_PROMPT,
    DEFAULT_MARKDOWN_EDIT_PROMPT,
    DEFAULT_MARKDOWN_SOURCE_EDIT_PROMPT,
    ChatCompletionsMarkdownEditor,
)
from app.modules.skill.domain.entities import (
    SkillDraftSourceOperation,
    SkillDraftSourceRun,
)
from app.modules.wiki_generation.domain.entities import SemanticPacket
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ChatClientConfig,
    ChatCompletionsJsonClient,
    GenericChatCompletionsExtractor,
)

REQUIRED_EXTRACTION_KEYS = {
    "chunk_id",
    "semantic_summary",
    "key_points",
    "observations",
    "categories",
    "core_concepts",
    "section_candidates",
    "mentions",
    "evidence_claims",
    "needs_neighbor_context",
    "context_problem",
}


class _ProbeAssertionError(RuntimeError):
    pass


def run_provider_e2e(
    client: ChatCompletionsJsonClient,
    *,
    prompt_root: Path,
) -> list[dict[str, object]]:
    return [
        _run_probe(
            "ingestion_json",
            lambda: _probe_ingestion(client, prompt_root),
        ),
        _run_probe(
            "agent_router",
            lambda: _probe_agent_router(client),
        ),
        _run_probe(
            "agent_executors",
            lambda: _probe_agent_executors(client),
        ),
        _run_probe(
            "markdown_create",
            lambda: _probe_markdown_create(client),
        ),
    ]


def _probe_ingestion(
    client: ChatCompletionsJsonClient,
    prompt_root: Path,
) -> None:
    extraction = GenericChatCompletionsExtractor(
        client,
        (prompt_root / "semantic_extraction.system.md").read_text(
            encoding="utf-8"
        ),
    ).extract(
        SemanticPacket(
            chunk_id="provider-e2e",
            document_id="provider-e2e",
            block_ids=["B0001"],
            text="[B0001] RAG는 검색한 근거를 사용해 답변을 생성한다.",
        )
    )
    missing_keys = sorted(REQUIRED_EXTRACTION_KEYS - extraction.keys())
    if missing_keys:
        raise RuntimeError(
            f"Ingestion JSON contract missing keys: {', '.join(missing_keys)}"
        )


def _probe_agent_router(client: ChatCompletionsJsonClient) -> None:
    router = ChatCompletionsTurnRouter(
        client,
        Path(DEFAULT_AGENT_TURN_ROUTER_PROMPT).read_text(encoding="utf-8"),
    )
    active_markdown = ActiveMarkdownContext(
        markdown="# 저장소 결정\n\nMongoDB 사용 여부",
        target=MarkdownEditTarget(
            type="whole_document",
            start_line=1,
            end_line=3,
        ),
    )
    selected_work = SkillDraftSourceRun(
        run_id="provider-e2e-run",
        status="completed",
        request_summary="워크스페이스 문서를 정리했습니다.",
        plan_summary="문서를 이동했습니다.",
        successful_operations=(
            SkillDraftSourceOperation(
                tool_name="move_document",
                reason="문서를 대상 폴더로 이동",
            ),
        ),
    )
    cases = (
        (
            AgentTurnRequest(message="RAG가 무엇인지 한 문장으로 설명해줘."),
            ("chat_answer", "workspace", "none", False, (), None, None, None),
            (),
        ),
        (
            AgentTurnRequest(
                message="Mongo DB를 사용하지 않기로 판단한 이유가 뭐지?",
                active_markdown_context=active_markdown,
            ),
            ("chat_answer", "workspace", "none", False, (), None, None, None),
            (),
        ),
        (
            AgentTurnRequest(
                message="Mongo가 이 문서를 저장하지 않는 이유가 뭐지?",
                active_markdown_context=active_markdown,
            ),
            ("chat_answer", "workspace", "none", False, (), None, None, None),
            (),
        ),
        (
            AgentTurnRequest(
                message="현재 문서를 요약한 뒤 보관 폴더로 옮겨 저장해줘",
                active_markdown_context=active_markdown,
            ),
            (
                "workspace_workflow",
                "none",
                "edit",
                True,
                ("document-edit", "folder-organize"),
                "shorten",
                "replace",
                "target",
            ),
            (),
        ),
        (
            AgentTurnRequest(
                message="현재 문서를 요약해서 저장해줄래?",
                active_markdown_context=active_markdown,
            ),
            (
                "workspace_workflow",
                "none",
                "edit",
                True,
                ("document-edit",),
                "shorten",
                "replace",
                "target",
            ),
            (),
        ),
        (
            AgentTurnRequest(
                message="Wiki 근거 요약을 이 문서 아래에 추가해줘",
                active_markdown_context=active_markdown,
            ),
            (
                "markdown_edit",
                "workspace",
                "edit",
                False,
                ("document-edit",),
                "shorten",
                "insert_after",
                "document_end",
            ),
            (
                (
                    "markdown_edit",
                    "workspace",
                    "edit",
                    False,
                    ("document-edit",),
                    "other",
                    "insert_after",
                    "document_end",
                ),
                (
                    "markdown_edit",
                    "workspace",
                    "edit",
                    False,
                    ("document-edit",),
                    "bullet_list",
                    "insert_after",
                    "document_end",
                ),
            ),
        ),
        (
            AgentTurnRequest(
                message="선택한 완료 작업을 재사용 가능한 Skill로 만들어줘",
                skill_draft_sources=(selected_work,),
            ),
            (
                "skill_draft_proposal",
                "none",
                "none",
                False,
                (),
                None,
                None,
                None,
            ),
            (),
        ),
    )
    for case_index, (request, expected, acceptable) in enumerate(cases, start=1):
        try:
            route = router.route(request)
        except AgentTurnRouteContractError:
            raise RuntimeError(
                f"Agent router case {case_index} failed its output contract"
            ) from None
        actual = (
            route.action,
            route.retrieval_source,
            route.document_operation,
            route.persist,
            route.required_capabilities,
            route.edit_goal,
            route.edit_operation,
            route.edit_destination,
        )
        if actual not in (expected, *acceptable):
            raise _ProbeAssertionError(
                f"Agent router case {case_index} returned {actual!r}; "
                f"expected one of {(expected, *acceptable)!r}"
            )


def _probe_markdown_create(client: ChatCompletionsJsonClient) -> None:
    document = ChatCompletionsMarkdownEditor(
        client,
        Path(DEFAULT_MARKDOWN_EDIT_PROMPT).read_text(encoding="utf-8"),
        create_system_prompt=Path(DEFAULT_MARKDOWN_CREATE_PROMPT).read_text(
            encoding="utf-8"
        ),
        source_edit_system_prompt=Path(
            DEFAULT_MARKDOWN_SOURCE_EDIT_PROMPT
        ).read_text(encoding="utf-8"),
    ).generate_markdown(
        MarkdownCreateRequest(
            instruction="합의 내용을 Markdown 문서로 만들어줘.",
            conversation_summary=(
                "RAG는 검색한 근거를 사용해 답변을 생성한다."
            ),
        )
    ).document
    if not document.title or not document.summary or not document.markdown:
        raise RuntimeError("Markdown create contract returned an empty field")


def _probe_agent_executors(client: ChatCompletionsJsonClient) -> None:
    active_markdown = ActiveMarkdownContext(
        markdown="# 저장소 결정\n\nMongoDB는 사용하지 않는다.",
        target=MarkdownEditTarget(type="selection", start_line=3, end_line=3),
    )
    conversation_replier = ChatCompletionsConversationReplier(
        client,
        Path(DEFAULT_CONVERSATION_REPLY_PROMPT).read_text(encoding="utf-8"),
    )
    reply = conversation_replier.reply(
        AgentTurnRequest(message="'오늘도 차근차근 해보자'를 더 자연스럽게 바꿔줘.")
    )
    if not reply.strip():
        raise RuntimeError("Conversation executor returned an empty reply")

    editor = ChatCompletionsMarkdownEditor(
        client,
        Path(DEFAULT_MARKDOWN_EDIT_PROMPT).read_text(encoding="utf-8"),
        create_system_prompt=Path(DEFAULT_MARKDOWN_CREATE_PROMPT).read_text(encoding="utf-8"),
        source_edit_system_prompt=Path(DEFAULT_MARKDOWN_SOURCE_EDIT_PROMPT).read_text(encoding="utf-8"),
    )
    edit = editor.generate_edit(
        MarkdownEditRequest(
            instruction="요약 내용을 이 문서 아래에 추가해줘.",
            markdown=active_markdown.markdown,
            target=MarkdownEditTarget(
                type="whole_document",
                start_line=1,
                end_line=3,
            ),
            edit_goal="style_change",
            edit_operation="insert_after",
            edit_destination="document_end",
        )
    ).edit
    if edit.operation != "insert_after" or not edit.replacement_markdown.strip():
        raise RuntimeError("Markdown edit executor violated the routed operation")


def _run_probe(
    name: str,
    probe: Callable[[], None],
) -> dict[str, object]:
    started_at = time.perf_counter()
    try:
        probe()
    except Exception as error:
        return {
            "name": name,
            "passed": False,
            "elapsed_seconds": round(
                time.perf_counter() - started_at,
                2,
            ),
            "error_type": type(error).__name__,
            "http_status": _http_status(error),
            **(
                {"failure": str(error)}
                if isinstance(error, _ProbeAssertionError)
                else {}
            ),
        }
    return {
        "name": name,
        "passed": True,
        "elapsed_seconds": round(time.perf_counter() - started_at, 2),
    }


def _http_status(error: Exception) -> int | None:
    match = re.search(r"LLM API HTTP (\d{3})", str(error))
    return int(match.group(1)) if match else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provider별 ingestion·Agent·Markdown 실제 API smoke 검증"
    )
    parser.add_argument(
        "--provider",
        choices=SUPPORTED_LLM_PROVIDERS,
        required=True,
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument(
        "--prompt-root",
        default=str(Path(__file__).parent / "prompts"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    provider, model = resolve_llm_selection(args.provider, args.model)
    defaults = resolve_llm_provider_defaults(
        provider=provider,
        model=model,
    )
    if not defaults.api_key:
        raise SystemExit(
            f"Missing API key. Set {defaults.api_key_env} before provider E2E."
        )
    client = ChatCompletionsJsonClient(
        ChatClientConfig(
            api_key=defaults.api_key,
            model=defaults.model or args.model,
            temperature=None,
            timeout_seconds=args.timeout_seconds,
            json_mode=True,
            provider=defaults.provider,
        )
    )
    started_at = time.perf_counter()
    results = run_provider_e2e(
        client,
        prompt_root=Path(args.prompt_root),
    )
    passed = sum(bool(result["passed"]) for result in results)
    print(
        json.dumps(
            {
                "provider": defaults.provider,
                "model": defaults.model,
                "passed": passed,
                "total": len(results),
                "elapsed_seconds": round(
                    time.perf_counter() - started_at,
                    2,
                ),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
