import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from pydantic import ValidationError

from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
from app.modules.query.application.answer_query import AnswerQueryUseCase
from app.modules.query.domain.entities import (
    ConversationContext,
    ConversationMessage,
)
from app.modules.query.infrastructure.in_memory_wiki_repository import InMemoryWikiRepository
from app.workers import task_worker
from tests.modules.query.test_answer_query import (
    EmptyTextSearch,
    RecordingAnswerGenerator,
    ScoreSearch,
    source_page,
)


def test_event_uses_common_command_envelope() -> None:
    command = {
        "run_id": "run-1",
        "kind": "query",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "operation_id": None,
        "api_key": "api-secret",
        "tavily_api_key": "tavily-secret",
        "access_token": "access-secret",
        "db_password": "password-secret",
        "client_secret": "client-secret",
        "apiKey": "camel-api-secret",
        "accessToken": "camel-access-secret",
        "dbPassword": "camel-password-secret",
        "clientSecret": "camel-client-secret",
        "metadata": {
            "api_key": "nested-api-secret",
            "ordinary": {"value": "keep"},
        },
        "items": [
            {"secret": "list-secret", "ordinary": "keep"},
            {"ordinary": {"value": "keep-too"}},
        ],
        "max_tokens": 1024,
    }

    event = task_worker._event(command, "succeeded", {"answer": "ok"})

    assert event["event_id"] == "query:run-1:succeeded"
    assert event["request"] == {
        "run_id": "run-1",
        "kind": "query",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "operation_id": None,
        "metadata": {"ordinary": {"value": "keep"}},
        "items": [
            {"ordinary": "keep"},
            {"ordinary": {"value": "keep-too"}},
        ],
        "max_tokens": 1024,
    }
    assert event["payload"] == {"answer": "ok"}


def test_progress_event_uses_stable_sequence_id() -> None:
    command = {
        "run_id": "run-1",
        "kind": "query",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
    }

    event = task_worker._progress_event(
        command,
        sequence=2,
        stage="context_built",
        message="답변 컨텍스트를 구성했습니다.",
        data={"evidence_count": 3},
    )

    assert event["event_id"] == "query:run-1:progress:2:context_built"
    assert event["status"] == "progress"
    # 소비 측이 읽지 않는 command 원본을 단계마다 반복 전송하지 않는다.
    assert "request" not in event
    assert event["payload"] == {
        "stage": "context_built",
        "message": "답변 컨텍스트를 구성했습니다.",
        "data": {"evidence_count": 3},
    }


def test_kafka_query_event_publisher_sends_progress_immediately() -> None:
    async def run_test() -> None:
        producer = MagicMock()
        producer.send_and_wait = AsyncMock()
        command = {"run_id": "run-1", "kind": "query"}
        publisher = task_worker.KafkaQueryEventPublisher(
            producer,
            asyncio.get_running_loop(),
            command,
        )

        await asyncio.to_thread(
            publisher.publish,
            "wiki_loaded",
            "Wiki 데이터를 불러왔습니다.",
            {"page_count": 3},
        )

        producer.send_and_wait.assert_awaited_once()
        topic, event = producer.send_and_wait.await_args.args
        assert topic == task_worker.RESULT_TOPIC
        assert event["event_id"] == "query:run-1:progress:1:wiki_loaded"
        assert event["payload"]["stage"] == "wiki_loaded"
        assert producer.send_and_wait.await_args.kwargs["key"] == b"run-1"

    asyncio.run(run_test())


def test_kafka_query_event_publisher_gives_up_when_broker_does_not_answer() -> None:
    """진행 이벤트 하나가 답변 생성 스레드를 무기한 붙잡으면 안 된다."""

    async def run_test() -> None:
        started = asyncio.Event()

        async def never_answers(*args: object, **kwargs: object) -> None:
            started.set()
            await asyncio.Event().wait()

        producer = MagicMock()
        producer.send_and_wait = never_answers
        publisher = task_worker.KafkaQueryEventPublisher(
            producer,
            asyncio.get_running_loop(),
            {"run_id": "run-1", "kind": "query"},
        )
        publisher.SEND_TIMEOUT_SECONDS = 0.05

        with pytest.raises(TimeoutError):
            await asyncio.to_thread(publisher.publish, "wiki_loaded", "Wiki 데이터를 불러왔습니다.")

        assert started.is_set()

    asyncio.run(run_test())


@pytest.mark.parametrize("allow_web_search", [False, True])
def test_query_command_passes_runtime_model_and_web_search_flag(
    allow_web_search: bool,
) -> None:
    command = {
        "run_id": "run-1",
        "kind": "query",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "question": "질문",
        "provider": "openai",
        "model": "  gpt-5-nano  ",
        "allow_web_search": allow_web_search,
        "recent_messages": [
            {"role": "user", "content": "이전 질문"},
            {"role": "assistant", "content": "이전 답변"},
        ],
        "api_key": "command-secret",
        "api_base_url": "https://command.example/v1",
        "tavily_api_key": "command-tavily-secret",
    }
    use_case = MagicMock()

    with (
        patch.object(
            task_worker,
            "build_answer_query_use_case",
            return_value=use_case,
        ) as build_use_case,
        patch.object(task_worker, "query_to_response") as to_response,
    ):
        to_response.return_value.model_dump.return_value = {"answer": "ok"}
        result = task_worker._handle_query(command)

    build_use_case.assert_called_once_with(
        provider="openai",
        model="gpt-5-nano",
        allow_web_search=allow_web_search,
        event_publisher=None,
    )
    use_case.execute.assert_called_once_with(
        "질문",
        workspace_id="workspace-1",
        user_id="user-1",
        conversation_context=ConversationContext(
            recent_messages=(
                ConversationMessage(role="user", content="이전 질문"),
                ConversationMessage(role="assistant", content="이전 답변"),
            )
        ),
        allow_web_search=allow_web_search,
    )
    assert result == {"answer": "ok"}


def test_query_command_passes_accumulated_conversation_summary() -> None:
    """요약을 넘겨야 이번 턴 요약이 앞 내용을 이어받는다. 빠뜨리면 매 턴 새로 쓰여 앞쪽이 사라진다."""
    command = {
        "run_id": "run-1",
        "kind": "query",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "question": "질문",
        "provider": "openai",
        "model": "gpt-5-nano",
        "allow_web_search": False,
        "recent_conversation_summary": "지금까지 검색 인덱싱을 다뤘다.",
        "recent_messages": [{"role": "user", "content": "이전 질문"}],
    }
    use_case = MagicMock()

    with (
        patch.object(task_worker, "build_answer_query_use_case", return_value=use_case),
        patch.object(task_worker, "query_to_response") as to_response,
    ):
        to_response.return_value.model_dump.return_value = {"answer": "ok"}
        task_worker._handle_query(command)

    assert use_case.execute.call_args.kwargs["conversation_context"] == ConversationContext(
        recent_conversation_summary="지금까지 검색 인덱싱을 다뤘다.",
        recent_messages=(ConversationMessage(role="user", content="이전 질문"),),
    )


def test_query_command_omits_conversation_context_without_history() -> None:
    command = {
        "run_id": "run-1",
        "kind": "query",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "question": "첫 질문",
        "provider": "openai",
        "model": "gpt-5-nano",
        "allow_web_search": False,
    }
    use_case = MagicMock()

    with (
        patch.object(task_worker, "build_answer_query_use_case", return_value=use_case),
        patch.object(task_worker, "query_to_response") as to_response,
    ):
        to_response.return_value.model_dump.return_value = {"answer": "ok"}
        task_worker._handle_query(command)

    assert use_case.execute.call_args.kwargs["conversation_context"] is None


@pytest.mark.parametrize(
    ("allow_web_search", "expected_telemetry"),
    [
        (False, (False, False, None)),
        (True, (True, False, "web_search_unavailable")),
    ],
)
def test_query_command_preserves_web_search_telemetry_for_low_relevance_without_adapter(
    allow_web_search: bool,
    expected_telemetry: tuple[bool, bool, str | None],
) -> None:
    command = {
        "run_id": "run-1",
        "kind": "query",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "question": "외부 정보가 뭐야?",
        "provider": "openai",
        "model": "gpt-5-nano",
        "allow_web_search": allow_web_search,
    }
    use_case = AnswerQueryUseCase(
        wiki_repository=InMemoryWikiRepository([source_page("source:seed", "Seed Source")], []),
        embedding_search=ScoreSearch({"Seed Source": 0.10}),
        text_search=EmptyTextSearch(),
        answer_generator=RecordingAnswerGenerator(),
        min_internal_relevance_score=0.50,
    )

    with patch.object(task_worker, "build_answer_query_use_case", return_value=use_case):
        result = task_worker._handle_query(command)

    assert (
        result["web_search_requested"],
        result["web_search_executed"],
        result["error_code"],
    ) == expected_telemetry


def test_query_command_requires_boolean_web_search_flag() -> None:
    command = {
        "run_id": "run-1",
        "kind": "query",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "question": "질문",
        "provider": "openai",
        "model": "  gpt-5-nano  ",
        "allow_web_search": "false",
    }

    with pytest.raises(ValueError, match="must be a boolean"):
        task_worker._handle_query(command)


@pytest.mark.parametrize(
    ("handler", "command"),
    [
        (
            task_worker._handle_query,
            {
                "run_id": "run-1",
                "workspace_id": "workspace-1",
                "user_id": "user-1",
                "session_id": "session-1",
                "question": "질문",
                "provider": "openai",
                "allow_web_search": False,
            },
        ),
        (
            task_worker._handle_lint,
            {
                "run_id": "run-1",
                "workspace_id": "workspace-1",
                "user_id": "user-1",
                "provider": "openai",
            },
        ),
    ],
)
def test_commands_require_runtime_model(handler, command: dict) -> None:
    with pytest.raises(ValueError, match="model"):
        handler(command)


def test_lint_command_passes_runtime_model_without_command_overrides() -> None:
    command = {
        "run_id": "run-1",
        "kind": "lint",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "provider": "openai",
        "model": "  gpt-5-nano  ",
        "dry_run": True,
        "api_key": "command-secret",
        "api_base_url": "https://command.example/v1",
    }
    maintenance = MagicMock()
    maintenance.lint.return_value = {"ok": True}

    with (
        patch.object(task_worker.database, "get_pipeline_run", return_value=None),
        patch.object(task_worker.database, "create_pipeline_run"),
        patch.object(task_worker.database, "finish_pipeline_run"),
        patch.object(task_worker, "get_wiki_maintenance", return_value=maintenance),
        patch.object(task_worker.WikiLintOut, "model_validate") as validate_out,
    ):
        validate_out.return_value.model_dump.return_value = {"ok": True}
        result = task_worker._handle_lint(command)

    lint_command = maintenance.lint.call_args.args[0]
    assert lint_command.provider == "openai"
    assert lint_command.model == "gpt-5-nano"
    assert result == {"ok": True}


def test_restore_rejects_changed_contribution_manifest() -> None:
    command = {
        "workspace_id": "workspace-1",
        "expected_contributions": {"page-1": ["op-1:1:1"]},
    }
    rows = [{"page_id": "page-1", "ingest_operation_id": "op-2", "sequence_revision": 2, "active": True}]

    with (
        patch.object(task_worker, "read_contributions", return_value=rows),
        pytest.raises(ValueError, match="manifest is stale"),
    ):
        task_worker._validate_restore_contributions(command)


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported AI command kind"):
        task_worker._handle({"run_id": "run-1", "kind": "unknown"})


def test_post_ingest_command_retries_in_process() -> None:
    command = {"run_id": "post-run-1", "kind": "post_ingest"}
    result = {"quality_status": "ready"}

    with (
        patch.object(
            task_worker,
            "_handle_post_ingest",
            side_effect=[
                RuntimeError("evidence evaluator omitted case indexes: [1, 2]"),
                result,
            ],
        ) as handle,
        patch.object(task_worker, "_failure_is_durable", return_value=False) as durable,
    ):
        assert task_worker._handle(command) == result

    assert handle.call_count == 2
    durable.assert_called_once_with(command)


def test_post_ingest_command_does_not_retry_durable_failure() -> None:
    command = {"run_id": "post-run-1", "kind": "post_ingest"}

    with (
        patch.object(
            task_worker,
            "_handle_post_ingest",
            side_effect=RuntimeError("post_ingest run failed"),
        ) as handle,
        patch.object(task_worker, "_failure_is_durable", return_value=True),
        pytest.raises(RuntimeError, match="post_ingest run failed"),
    ):
        task_worker._handle(command)

    handle.assert_called_once_with(command)


@pytest.mark.parametrize(
    ("embedding_mode", "runs_embedding"),
    [("bge-m3", True), ("text-only", False)],
)
def test_post_ingest_runs_configured_embedding_before_quality_evaluation(
    embedding_mode: str,
    runs_embedding: bool,
) -> None:
    command = {
        "run_id": "post-run-1",
        "ingest_run_id": "ingest-run-1",
        "kind": "post_ingest",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "provider": "openai",
        "model": "gpt-5-nano",
        "cluster_normalized": {"document": {"document_id": "document-1"}},
        "page_ids": ["page-1"],
    }
    calls = []
    artifact = {"maintenance_summary": {"promotion_candidate_count": 0}}
    parent = {
        "status": "succeeded",
        "manifest": {
            "post_ingest": {
                "cluster_normalized": command["cluster_normalized"],
                "page_ids": command["page_ids"],
                "quality_required": True,
                "generation_evaluation_status": "passed",
            },
            "source_blocks": [{"block_id": "B0001", "text": "근거"}],
        },
    }

    with (
        patch.dict(
            task_worker.os.environ,
            {"QUERY_EMBEDDING_MODE": embedding_mode},
        ),
        patch.object(
            task_worker.database,
            "get_pipeline_run",
            side_effect=lambda run_id: parent if run_id == "ingest-run-1" else None,
        ),
        patch.object(task_worker.database, "create_pipeline_run"),
        patch.object(
            task_worker.database,
            "checkpoint_pipeline_run",
            side_effect=lambda *_args: calls.append("checkpoint"),
        ),
        patch.object(task_worker.database, "update_pipeline_run_post_ingest"),
        patch.object(task_worker.database, "update_pipeline_run_cluster_contribution"),
        patch.object(
            task_worker,
            "build_post_ingest_cluster_artifact",
            side_effect=lambda **_kwargs: calls.append("cluster") or artifact,
        ),
        patch.object(task_worker, "_post_ingest_client", return_value=MagicMock()),
        patch.object(task_worker, "read_optional_text_object", return_value=""),
        patch.object(
            task_worker.database,
            "persist_post_ingest_clusters",
            side_effect=lambda *_args: calls.append("persist") or [],
        ),
        patch.object(
            task_worker,
            "build_wiki_embeddings",
            side_effect=lambda _ids: calls.append("embedding")
            or {
                "target_count": 1,
                "embedded_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
            },
        ),
        patch.object(
            task_worker,
            "_run_post_ingest_wiki_quality_evaluation",
            side_effect=lambda *_args: calls.append("quality")
            or [
                {"question": f"질문 {index}", "passed": True}
                for index in range(3)
            ],
        ),
        patch.object(task_worker.database, "finish_pipeline_run") as finish,
    ):
        result = task_worker._handle_post_ingest(command)

    assert result["quality_status"] == "ready"
    assert calls.index("cluster") < calls.index("persist")
    assert calls.index("persist") < calls.index("quality")
    if runs_embedding:
        assert calls.index("persist") < calls.index("embedding")
        assert calls.index("embedding") < calls.index("quality")
    else:
        assert "embedding" not in calls
        assert result["embedding"] == {
            "target_count": 1,
            "embedded_count": 0,
            "skipped_count": 1,
            "failed_count": 0,
        }
    finish.assert_called_once()


def test_post_ingest_marks_needs_review_after_third_failure() -> None:
    command = {
        "run_id": "post-run-1",
        "ingest_run_id": "ingest-run-1",
        "kind": "post_ingest",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "provider": "openai",
        "model": "gpt-5-nano",
        "cluster_normalized": {},
        "page_ids": ["page-1"],
    }
    existing = {
        "status": "running",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "manifest": {
            "attempt": 2,
            "cluster_artifact": {},
            "cluster_applied": True,
            "page_ids": ["page-1"],
        },
    }

    parent = {
        "status": "succeeded",
        "manifest": {
            "post_ingest": {
                "cluster_normalized": {},
                "page_ids": ["page-1"],
            }
        },
    }
    with (
        patch.dict(
            task_worker.os.environ,
            {"QUERY_EMBEDDING_MODE": "bge-m3"},
        ),
        patch.object(
            task_worker.database,
            "get_pipeline_run",
            side_effect=lambda run_id: parent if run_id == "ingest-run-1" else existing,
        ),
        patch.object(task_worker.database, "checkpoint_pipeline_run"),
        patch.object(task_worker.database, "update_pipeline_run_post_ingest") as update_parent,
        patch.object(
            task_worker,
            "build_wiki_embeddings",
            side_effect=RuntimeError("embedding unavailable"),
        ),
        patch.object(task_worker.database, "fail_pipeline_run") as fail,
        pytest.raises(RuntimeError, match="embedding unavailable"),
    ):
        task_worker._handle_post_ingest(command)

    fail.assert_called_once_with("post-run-1", "embedding unavailable")
    assert update_parent.call_args.args[1]["status"] == "needs_review"


def test_post_ingest_quality_uses_source_and_concept_rag_with_source_evaluator() -> None:
    use_case = MagicMock()
    use_case.retrieve_evidence.return_value = SimpleNamespace(
        evidence_snippets=[
            SimpleNamespace(
                rank=1,
                source_document_id="document-1",
                source_block_ids=["B0001"],
                source_refs=[
                    SimpleNamespace(
                        source_document_id="document-1",
                        source_block_id="B0001",
                    ),
                    SimpleNamespace(
                        source_document_id="document-2",
                        source_block_id="B0002",
                    ),
                ],
                text="근거",
            )
        ],
        retrieval_summary=SimpleNamespace(
            source_candidate_count=1,
            concept_candidate_count=2,
            used_source_count=1,
            used_concept_count=1,
        ),
    )
    command = {
        "run_id": "post-run-1",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "provider": "openai",
        "model": "gpt-5-nano",
    }
    state = {}
    quality_cases = [
        {
            "question": "질문",
            "expected_claims": ["근거"],
            "source_document_id": "document-1",
            "source_block_ids": ["B0001"],
        }
    ]
    with (
        patch.object(
            task_worker,
            "build_answer_query_use_case",
            return_value=use_case,
        ) as build_query,
        patch.object(task_worker, "_post_ingest_client", return_value=MagicMock()),
        patch.object(
            task_worker.database,
            "list_source_blocks",
            return_value=[{"document_id": "document-1", "block_id": "B0001", "text": "근거"}],
        ) as load_question_source,
        patch.object(
            task_worker,
            "generate_post_ingest_quality_cases",
            return_value=quality_cases,
        ) as generate_cases,
        patch.object(
            task_worker.database,
            "list_source_blocks_for_refs",
            return_value=[
                {
                    "source_document_id": "document-1",
                    "block_id": "B0001",
                    "text": "근거",
                },
                {
                    "source_document_id": "document-2",
                    "block_id": "B0002",
                    "text": "추가 근거",
                },
            ],
        ) as load_source_blocks,
        patch.object(
            task_worker,
            "evaluate_post_ingest_evidence",
            return_value=[
                {
                    "passed": True,
                    "evidence_recall": 1.0,
                    "evidence_precision": 1.0,
                    "source_alignment": 1.0,
                }
            ],
        ) as evaluate,
        patch.object(task_worker.database, "checkpoint_pipeline_run"),
    ):
        evaluations = task_worker._run_post_ingest_wiki_quality_evaluation(command, state)

    build_arguments = build_query.call_args.kwargs
    repository = build_arguments.pop("wiki_repository")
    assert repository._concept_only is False
    assert build_arguments == {
        "provider": "openai",
        "model": "gpt-5-nano",
        "allow_web_search": False,
        "evaluator_mode": "disabled",
    }
    assert evaluations[0]["passed"] is True
    assert "answer" not in evaluations[0]
    assert evaluations[0]["gold_ref_hit"] is True
    assert evaluations[0]["gold_ref_rank"] == 1
    assert evaluations[0]["evidence_snippets"] == [
        {
            "rank": 1,
            "source_document_id": "document-1",
            "source_block_ids": ["B0001"],
            "source_refs": [
                {
                    "source_document_id": "document-1",
                    "source_block_id": "B0001",
                },
                {
                    "source_document_id": "document-2",
                    "source_block_id": "B0002",
                },
            ],
            "text": "근거",
        }
    ]
    assert evaluations[0]["retrieval_summary"] == {
        "source_candidate_count": 1,
        "concept_candidate_count": 2,
        "used_source_count": 1,
        "used_concept_count": 1,
    }
    load_question_source.assert_called_once_with("document-1")
    generate_cases.assert_called_once()
    load_source_blocks.assert_called_once_with(
        ["document-1:B0001", "document-2:B0002"]
    )
    evaluate.assert_called_once()
    use_case.retrieve_evidence.assert_called_once_with(
        "질문",
        workspace_id="workspace-1",
        user_id="user-1",
    )
    use_case.execute.assert_not_called()


def test_post_ingest_quality_retry_reuses_checkpointed_batch() -> None:
    command = {
        "run_id": "post-run-1",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "provider": "openai",
        "model": "gpt-5-nano",
    }
    state = {
        "wiki_quality_evaluations": [
            {"question": "첫 번째 질문", "passed": True}
        ]
    }

    evaluations = task_worker._run_post_ingest_wiki_quality_evaluation(
        command,
        state,
    )

    assert evaluations == [{"question": "첫 번째 질문", "passed": True}]


def test_post_ingest_quality_retries_when_three_aligned_cases_are_unavailable() -> None:
    command = {
        "run_id": "post-run-1",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "provider": "openai",
        "model": "gpt-5-nano",
    }
    state = {}

    with (
        patch.object(task_worker, "_post_ingest_client", return_value=MagicMock()),
        patch.object(
            task_worker.database,
            "list_source_blocks",
            return_value=[{"block_id": "B0001", "text": "원문"}],
        ),
        patch.object(
            task_worker,
            "generate_post_ingest_quality_cases",
            return_value=[],
        ),
        patch.object(task_worker.database, "checkpoint_pipeline_run") as checkpoint,
        pytest.raises(RuntimeError, match="평가 질문을 생성하지 못했습니다"),
    ):
        task_worker._run_post_ingest_wiki_quality_evaluation(command, state)

    assert "quality_cases" not in state
    checkpoint.assert_not_called()


@pytest.mark.parametrize(
    ("generation_status", "evaluation_count"),
    [("unresolved", 3), ("passed", 1)],
)
def test_post_ingest_requires_source_and_wiki_quality_to_be_ready(
    generation_status: str,
    evaluation_count: int,
) -> None:
    evaluations = [
        {"question": f"질문 {index}", "passed": True}
        for index in range(evaluation_count)
    ]
    command = {
        "run_id": "post-run-1",
        "ingest_run_id": "ingest-run-1",
        "kind": "post_ingest",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "provider": "openai",
        "model": "gpt-5-nano",
    }
    parent = {
        "status": "succeeded",
        "manifest": {
            "post_ingest": {
                "cluster_normalized": {},
                "page_ids": [],
                "quality_required": True,
                "generation_evaluation_status": generation_status,
            },
            "source_blocks": [],
        },
    }

    with (
        patch.object(
            task_worker.database,
            "get_pipeline_run",
            side_effect=lambda run_id: parent if run_id == "ingest-run-1" else None,
        ),
        patch.object(task_worker.database, "create_pipeline_run"),
        patch.object(task_worker.database, "checkpoint_pipeline_run"),
        patch.object(task_worker.database, "update_pipeline_run_post_ingest"),
        patch.object(task_worker.database, "update_pipeline_run_cluster_contribution"),
        patch.object(task_worker, "build_post_ingest_cluster_artifact", return_value={}),
        patch.object(task_worker, "_post_ingest_client", return_value=MagicMock()),
        patch.object(task_worker, "read_optional_text_object", return_value=""),
        patch.object(task_worker.database, "persist_post_ingest_clusters", return_value=[]),
        patch.object(
            task_worker,
            "build_wiki_embeddings",
            return_value={
                "target_count": 0,
                "embedded_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
            },
        ),
        patch.object(
            task_worker,
            "_run_post_ingest_wiki_quality_evaluation",
            return_value=evaluations,
        ),
        patch.object(task_worker.database, "finish_pipeline_run"),
    ):
        result = task_worker._handle_post_ingest(command)

    assert result["quality_status"] == "needs_review"
    assert result["generation_evaluation_status"] == generation_status


@pytest.mark.parametrize("kind", ["query", "post_ingest", "lint", "restore_ingest", "restore_lint"])
@pytest.mark.parametrize("status", [None, "running", "failed", "cancel_requested", "rolling_back", "rollback_failed", "cancelled"])
def test_failure_is_durable_only_with_registered_terminal_or_rollback_state(kind, status):
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {"status": status} if status else None
    context = MagicMock()
    context.__enter__.return_value = connection
    with patch("app.modules.task_cancellation.infrastructure.postgres_task_journal.connect", return_value=context):
        assert task_worker._failure_is_durable({"run_id": "run", "kind": kind}) is (status not in {None, "running"})


@pytest.mark.parametrize(
    ("skill_mode", "skill_id"),
    [("auto", None), ("explicit", "skill-1"), ("off", None)],
)
def test_agent_command_copies_skill_draft_fields_into_agent_request(
    skill_mode: str, skill_id: str | None,
) -> None:
    command = {
        "run_id": "agent_0123456789abcdef0123456789abcdef",
        "kind": "agent",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "document_id": "document-1",
        "base_version": 7,
        "apply_operation_id": "op-1",
        "message": "Skill로 만들어줘",
        "provider": "openai",
        "model": "gpt-5-nano",
        "skill_mode": skill_mode,
        "skill_id": skill_id,
        "conversation_context": None,
        "editor_snapshot": {"markdown": "# 제목"},
        "skill_draft_sources": [{
            "run_id": "agent_source",
            "status": "completed",
            "request_summary": "정식 요청",
            "plan_summary": "정식 계획",
            "successful_operations": [{"tool_name": "move_document", "reason": "정식 이유"}],
        }],
        "skill_draft_user_directives": ["일반화해줘"],
        "skill_draft_excluded_literals": ["secret-doc"],
        "skill_scope_type": "team",
    }
    use_case = MagicMock()
    response = MagicMock()
    response.model_dump.return_value = {"ok": True}
    connection = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = connection

    with (
        patch.object(task_worker, "_register_agent_command", return_value=("execute", None)),
        patch.object(task_worker, "build_handle_agent_turn_use_case", return_value=use_case),
        patch.object(task_worker, "agent_to_response", return_value=response),
        patch.object(task_worker.database, "connect_ai", return_value=context),
    ):
        assert task_worker._handle_agent(command) == {"ok": True}

    request = use_case.execute.call_args.args[0]
    assert request.skill_draft_sources[0].request_summary == "정식 요청"
    assert request.skill_draft_sources[0].successful_operations[0].tool_name == "move_document"
    assert request.skill_draft_user_directives == ("일반화해줘",)
    assert request.skill_draft_excluded_literals == ("secret-doc",)
    assert request.skill_scope_type == "team"
    assert request.skill_mode == skill_mode
    assert request.skill_id == skill_id
    assert request.document_id == "document-1"
    assert request.base_version == 7


def test_agent_command_passes_web_search_flag_to_request_body() -> None:
    """질의 갈래로 판정됐을 때 웹 검색을 켤 수 있어야 한다. command에서 body까지 끊기면 안 된다."""
    command = {
        "run_id": "agent_0123456789abcdef0123456789abcdef",
        "kind": "agent",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "message": "최신 소식 알려줘",
        "provider": "openai",
        "model": "gpt-5-nano",
        "allow_web_search": True,
    }
    use_case = MagicMock()
    response = MagicMock()
    response.model_dump.return_value = {"ok": True}
    connection = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = connection

    with (
        patch.object(task_worker, "_register_agent_command", return_value=("execute", None)),
        patch.object(task_worker, "build_handle_agent_turn_use_case", return_value=use_case),
        patch.object(task_worker, "agent_to_response", return_value=response),
        patch.object(task_worker.database, "connect_ai", return_value=context),
    ):
        task_worker._handle_agent(command)

    assert use_case.execute.call_args.args[0].allow_web_search is True


def test_agent_command_rejects_auto_skill_id() -> None:
    command = {
        "run_id": "agent_0123456789abcdef0123456789abcdef",
        "kind": "agent",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "document_id": "document-1",
        "base_version": 7,
        "apply_operation_id": "op-1",
        "message": "정리해줘",
        "provider": "openai",
        "model": "gpt-5-nano",
        "skill_mode": "auto",
        "skill_id": "skill-1",
        "editor_snapshot": {"markdown": "# 제목"},
    }
    use_case = MagicMock()
    connection = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = connection

    with (
        patch.object(task_worker, "_register_agent_command", return_value=("execute", None)),
        patch.object(task_worker, "build_handle_agent_turn_use_case", return_value=use_case),
        patch.object(task_worker.database, "connect_ai", return_value=context),
        pytest.raises(ValidationError),
    ):
        task_worker._handle_agent(command)

    use_case.execute.assert_not_called()


def test_unregistered_agent_failure_is_not_durable() -> None:
    command = {"run_id": "run-1", "kind": "agent"}
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = None
    context = MagicMock()
    context.__enter__.return_value = connection

    with patch.object(task_worker.database, "connect_ai", return_value=context):
        assert task_worker._failure_is_durable(command) is False


def test_agent_route_contract_failure_keeps_specific_error_code() -> None:
    error = AgentTurnRouteContractError(["invalid route"])

    assert task_worker._agent_failure_code(error) == "agent_turn_route_contract_failed"


def test_disabled_agent_feature_is_not_an_invalid_user_request() -> None:
    from app.modules.agent_run.application.start_agent_run import StartAgentRunUseCase
    from app.modules.agent_run.domain.entities import StartAgentRunRequest

    repository = MagicMock()
    try:
        StartAgentRunUseCase(repository, feature_enabled=False).start(
            StartAgentRunRequest(
                workspace_id="workspace-1", user_id="user-1",
                instruction="현재까지 업로드 한 문서, 알맞은 폴더 이름 생성해서 주제별로 정리해 줘",
            )
        )
    except task_worker.AgentConfigurationError as error:
        assert task_worker._agent_failure_code(error) == "agent_not_configured"
        assert task_worker._agent_failure_result(error)["error_code"] == "agent_not_configured"
    else:
        raise AssertionError("비활성화된 기능은 설정 오류로 종료해야 한다.")
    repository.create_with_planning_job.assert_not_called()


def test_agent_route_contract_failure_persists_safe_diagnostic() -> None:
    command = {
        "run_id": "agent_route_failure",
        "kind": "agent",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "message": "문서를 보완해줘",
        "provider": "openai",
        "model": "gpt-5-nano",
    }
    error = AgentTurnRouteContractError(
        ["document_operation edit requires edit_operation"]
    )
    use_case = MagicMock()
    use_case.execute.side_effect = error
    connection = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = connection

    with (
        patch.object(task_worker, "_register_agent_command", return_value=("execute", None)),
        patch.object(task_worker, "build_handle_agent_turn_use_case", return_value=use_case),
        patch.object(task_worker.database, "connect_ai", return_value=context),
        pytest.raises(AgentTurnRouteContractError),
    ):
        task_worker._handle_agent(command)

    update = connection.execute.call_args_list[0]
    assert "result = %s" in update.args[0]
    assert update.args[1][1].obj == {
        "outcome": "failed",
        "error_code": "agent_turn_route_contract_failed",
        "failure_type": "AgentTurnRouteContractError",
        "contract_failures": ["document_operation edit requires edit_operation"],
    }


@pytest.mark.parametrize(
    ("provider", "model", "error_match"),
    [
        (None, "gpt-5-nano", "provider and model are required"),
        ("openai", "unsupported-model", "Unsupported model"),
        ("gemini", "gpt-5-nano", "Expected gemini-3.1-flash-lite"),
    ],
)
def test_invalid_agent_selection_is_terminally_registered(
    provider: str | None,
    model: str | None,
    error_match: str,
) -> None:
    command = {
        "run_id": "agent_invalid_selection",
        "kind": "agent",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "document_id": "document-1",
        "base_version": 7,
        "apply_operation_id": "op-1",
        "message": "문서를 정리해줘",
        "provider": provider,
        "model": model,
        "editor_snapshot": {"markdown": "# 제목"},
    }
    connection = MagicMock()
    inserted = MagicMock()
    inserted.fetchone.return_value = {"id": command["run_id"]}
    locked = MagicMock()
    locked.fetchone.return_value = {
        "status": "failed",
        "result": None,
        "command_envelope_hash": task_worker._agent_command_hash(command),
    }
    connection.execute.side_effect = [inserted, MagicMock(), MagicMock(), MagicMock(), locked]
    context = MagicMock()
    context.__enter__.return_value = connection

    with (
        patch.object(task_worker.database, "connect_ai", return_value=context),
        pytest.raises(ValueError, match=error_match),
    ):
        task_worker._handle_agent(command)

    queries = [call.args[0] for call in connection.execute.call_args_list]
    assert any("UPDATE agent_runs" in query and "status = 'failed'" in query for query in queries)
    assert any("UPDATE agent_jobs" in query and "status = 'failed'" in query for query in queries)
    assert context.__exit__.call_args.args[:3] == (None, None, None)

    durability_connection = MagicMock()
    durability_connection.execute.return_value.fetchone.return_value = {"status": "failed"}
    durability_context = MagicMock()
    durability_context.__enter__.return_value = durability_connection
    with patch.object(task_worker.database, "connect_ai", return_value=durability_context):
        assert task_worker._failure_is_durable(command) is True


def test_agent_command_without_document_registers_run_with_null_targets() -> None:
    """문서를 열지 않은 턴은 편집 대상이 없다. 컬럼이 nullable이라 그대로 비워 넣는다."""
    command = {
        "run_id": "agent_no_document",
        "kind": "agent",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "message": "아까 그거 다시 설명해줘",
        "provider": "openai",
        "model": "gpt-5-nano",
    }
    connection = MagicMock()
    inserted = MagicMock()
    inserted.fetchone.return_value = {"id": command["run_id"]}
    locked = MagicMock()
    locked.fetchone.return_value = {
        "status": "queued",
        "result": None,
        "command_envelope_hash": task_worker._agent_command_hash(command),
    }
    def execute(query: str, *args: object) -> MagicMock:
        if "INSERT INTO agent_runs" in query:
            return inserted
        if "FOR UPDATE" in query:
            return locked
        return MagicMock()

    connection.execute.side_effect = execute
    context = MagicMock()
    context.__enter__.return_value = connection

    with patch.object(task_worker.database, "connect_ai", return_value=context):
        state, replay = task_worker._register_agent_command(command)

    assert (state, replay) == ("execute", None)
    insert_params = connection.execute.call_args_list[0].args[1]
    assert insert_params[-4:-1] == (None, None, None)


def test_replayed_invalid_agent_selection_reuses_failed_run_without_new_job() -> None:
    command = {
        "run_id": "agent_invalid_replay",
        "kind": "agent",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "document_id": "document-1",
        "base_version": 7,
        "apply_operation_id": "op-1",
        "message": "문서를 정리해줘",
        "provider": "openai",
        "model": "unsupported-model",
        "editor_snapshot": {"markdown": "# 제목"},
    }

    first_connection = MagicMock()
    first_inserted = MagicMock()
    first_inserted.fetchone.return_value = {"id": command["run_id"]}
    first_locked = MagicMock()
    first_locked.fetchone.return_value = {
        "status": "failed",
        "result": None,
        "command_envelope_hash": task_worker._agent_command_hash(command),
    }
    first_connection.execute.side_effect = [
        first_inserted,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        first_locked,
    ]
    first_context = MagicMock()
    first_context.__enter__.return_value = first_connection

    second_connection = MagicMock()
    second_inserted = MagicMock()
    second_inserted.fetchone.return_value = None
    second_locked = MagicMock()
    second_locked.fetchone.return_value = {
        "status": "failed",
        "result": None,
        "command_envelope_hash": task_worker._agent_command_hash(command),
    }
    second_connection.execute.side_effect = [second_inserted, second_locked]
    second_context = MagicMock()
    second_context.__enter__.return_value = second_connection

    with patch.object(
        task_worker.database,
        "connect_ai",
        side_effect=[first_context, second_context],
    ):
        with pytest.raises(ValueError, match="Unsupported model"):
            task_worker._register_agent_command(command)
        with pytest.raises(ValueError, match="Unsupported model"):
            task_worker._register_agent_command(command)

    assert second_connection.execute.call_count == 2


def test_invalid_agent_payload_is_terminally_registered_after_validation_failure() -> None:
    command = {
        "run_id": "agent_invalid_payload",
        "kind": "agent",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "document_id": "document-1",
        "base_version": 7,
        "apply_operation_id": "op-1",
        "message": "문서를 정리해줘",
        "provider": "openai",
        "model": "gpt-5-nano",
        "editor_snapshot": {
            "markdown": "# 제목",
            "target": {"type": "selection", "start_line": 0, "end_line": 1},
        },
    }
    registration_connection = MagicMock()
    inserted = MagicMock()
    inserted.fetchone.return_value = {"id": command["run_id"]}
    queued = MagicMock()
    queued.fetchone.return_value = {
        "status": "queued",
        "result": None,
        "command_envelope_hash": task_worker._agent_command_hash(command),
    }
    registration_connection.execute.side_effect = [
        inserted,
        MagicMock(),
        queued,
        MagicMock(),
        MagicMock(),
    ]
    registration_context = MagicMock()
    registration_context.__enter__.return_value = registration_connection

    failure_connection = MagicMock()
    failure_context = MagicMock()
    failure_context.__enter__.return_value = failure_connection

    durability_connection = MagicMock()
    durability_connection.execute.return_value.fetchone.return_value = {"status": "failed"}
    durability_context = MagicMock()
    durability_context.__enter__.return_value = durability_connection

    with (
        patch.object(
            task_worker.database,
            "connect_ai",
            side_effect=[registration_context, failure_context, durability_context],
        ),
        pytest.raises(ValidationError),
    ):
        task_worker._handle_agent(command)

    failure_queries = [call.args[0] for call in failure_connection.execute.call_args_list]
    assert any("UPDATE agent_runs" in query and "status = 'failed'" in query for query in failure_queries)
    assert any("UPDATE agent_jobs" in query and "status = 'failed'" in query for query in failure_queries)
    assert registration_context.__exit__.call_args.args[:3] == (None, None, None)
    assert failure_context.__exit__.call_args.args[:3] == (None, None, None)
    with patch.object(task_worker.database, "connect_ai", return_value=durability_context):
        assert task_worker._failure_is_durable(command) is True


def test_agent_command_registers_supplied_run_and_deterministic_job_once() -> None:
    command = {
        "run_id": "agent_0123456789abcdef0123456789abcdef",
        "kind": "agent",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "document_id": "document-1",
        "base_version": 7,
        "apply_operation_id": "op-1",
        "message": "문서를 정리해줘",
        "provider": "gemini",
        "model": " gemini-3.1-flash-lite ",
        "editor_snapshot": {"markdown": "# 제목"},
    }
    connection = MagicMock()
    inserted = MagicMock()
    inserted.fetchone.return_value = {"id": command["run_id"]}
    locked = MagicMock()
    locked.fetchone.return_value = {
        **command,
        "id": command["run_id"],
        "status": "queued",
        "result": None,
        "command_envelope_hash": task_worker._agent_command_hash(command),
    }
    connection.execute.side_effect = [inserted, MagicMock(), locked, MagicMock(), MagicMock()]
    context = MagicMock()
    context.__enter__.return_value = connection

    with patch.object(task_worker.database, "connect_ai", return_value=context):
        state, result = task_worker._register_agent_command(command)

    assert state == "execute"
    assert result is None
    run_insert = connection.execute.call_args_list[0]
    assert "provider, model" in run_insert.args[0]
    assert run_insert.args[1][4:6] == ("gemini", "gemini-3.1-flash-lite")
    job_insert = connection.execute.call_args_list[1]
    assert job_insert.args[1][0] == f"{command['run_id']}:markdown_turn"


def test_repeated_agent_command_rejects_changed_envelope() -> None:
    command = {
        "run_id": "agent_0123456789abcdef0123456789abcdef",
        "kind": "agent",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "document_id": "document-1",
        "base_version": 7,
        "apply_operation_id": "op-1",
        "message": "변경된 요청",
        "provider": "openai",
        "model": "gpt-5-nano",
        "editor_snapshot": {"markdown": "# 제목"},
    }
    connection = MagicMock()
    inserted = MagicMock()
    inserted.fetchone.return_value = None
    locked = MagicMock()
    locked.fetchone.return_value = {
        "id": command["run_id"],
        "status": "completed",
        "result": {"ok": True},
        "command_envelope_hash": "different",
    }
    connection.execute.side_effect = [inserted, locked]
    context = MagicMock()
    context.__enter__.return_value = connection

    with (
        patch.object(task_worker.database, "connect_ai", return_value=context),
        pytest.raises(ValueError, match="envelope"),
    ):
        task_worker._register_agent_command(command)


def test_repeated_identical_agent_command_reuses_completed_result() -> None:
    command = {
        "run_id": "agent_0123456789abcdef0123456789abcdef",
        "kind": "agent",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "document_id": "document-1",
        "base_version": 7,
        "apply_operation_id": "op-1",
        "message": "문서를 정리해줘",
        "provider": "openai",
        "model": "gpt-5-nano",
        "editor_snapshot": {"markdown": "# 제목"},
    }
    connection = MagicMock()
    inserted = MagicMock()
    inserted.fetchone.return_value = None
    locked = MagicMock()
    locked.fetchone.return_value = {
        "status": "completed",
        "result": {"edit": {"changed": True}},
        "command_envelope_hash": task_worker._agent_command_hash(command),
    }
    connection.execute.side_effect = [inserted, locked]
    context = MagicMock()
    context.__enter__.return_value = connection

    with patch.object(task_worker.database, "connect_ai", return_value=context):
        state, result = task_worker._register_agent_command(command)

    assert state == "completed"
    assert result == {"edit": {"changed": True}}
    assert connection.execute.call_count == 2
