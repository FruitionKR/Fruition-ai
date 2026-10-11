import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)
from app.modules.wiki_ingestion.infrastructure.workspace_concept_lock import ConceptLockTimeout
from app.workers import ingest_worker


def test_create_pipeline_run_ignores_duplicate_run_id() -> None:
    connection = MagicMock()

    with patch.object(database, "connect", return_value=connection):
        database.create_pipeline_run(
            "run-1",
            "document-1",
            "user-1",
            "workspace-1",
            "storage:document-1.md",
            "runs/run-1",
            "api",
        )

    query = connection.__enter__.return_value.execute.call_args.args[0]
    assert "ON CONFLICT (id) DO NOTHING" in query


@pytest.mark.parametrize("status", ["succeeded", "failed", "notify_pending"])
def test_handle_skips_terminal_redelivered_run(status: str) -> None:
    repository = MagicMock()
    repository.get_run.return_value = {"status": status}
    command = {
        "run_id": "run-1",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "provider": "openai",
        "model": "gpt-5-nano",
    }

    with (
        patch.object(ingest_worker, "get_pipeline_run_repository", return_value=repository),
        patch.object(ingest_worker, "_run_pipeline_request") as run_request,
    ):
        ingest_worker._handle(command)

    run_request.assert_not_called()


def test_handle_retries_running_redelivered_run() -> None:
    repository = MagicMock()
    repository.get_run.return_value = {"status": "running"}
    command = {
        "run_id": "run-1",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "provider": "openai",
        "model": "gpt-5-nano",
    }

    with (
        patch.object(ingest_worker, "get_pipeline_run_repository", return_value=repository),
        patch.object(ingest_worker, "get_deferred_pipeline_run_use_case") as get_use_case,
        patch.object(ingest_worker, "get_pipeline_source_reader") as get_source_reader,
        patch.object(ingest_worker, "_run_pipeline_request") as run_request,
    ):
        ingest_worker._handle(command)

    run_request.assert_called_once_with(
        ingest_worker._build_payload(command),
        background_tasks=None,
        use_case=get_use_case.return_value,
        repository=repository,
        source_reader=get_source_reader.return_value,
        run_id="run-1",
    )
    get_use_case.assert_called_once_with()


def test_handle_rejects_command_that_reuses_another_run_context() -> None:
    repository = MagicMock()
    repository.get_run.return_value = {
        "status": "running",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
    }

    with patch.object(
        ingest_worker,
        "get_pipeline_run_repository",
        return_value=repository,
    ):
        with pytest.raises(ValueError, match="registered run context"):
            ingest_worker._handle(
                {
                    "run_id": "run-1",
                    "document_id": "document-2",
                    "workspace_id": "workspace-1",
                    "user_id": "user-1",
                    "provider": "openai",
                }
            )

    repository.fail.assert_called_once_with(
        "run-1", "ingest command does not match the registered run context"
    )


def test_handle_records_failure_after_run_registration() -> None:
    repository = MagicMock()
    repository.get_run.return_value = None
    command = {
        "run_id": "run-1",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "provider": "openai",
        "model": "gpt-5-nano",
    }

    with (
        patch.object(ingest_worker, "get_pipeline_run_repository", return_value=repository),
        patch.object(
            ingest_worker,
            "_run_pipeline_request",
            side_effect=RuntimeError("document lookup failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="document lookup failed"):
            ingest_worker._handle(command)

    repository.fail.assert_called_once_with("run-1", "document lookup failed")


def test_build_payload_requires_actor_context() -> None:
    with pytest.raises(ValueError, match="user_id and workspace_id"):
        ingest_worker._build_payload({"run_id": "run-1", "document_id": "document-1"})


def test_build_payload_uses_workspace_command_snapshot() -> None:
    payload = ingest_worker._build_payload(
        {
            "run_id": "run-1",
            "kind": "document",
            "document_id": "document-1",
            "workspace_id": "workspace-1",
            "user_id": "user-1",
            "provider": "openai",
            "model": "  gpt-5-nano  ",
            "api_key": "command-secret",
            "api_base_url": "https://command.example/v1",
        }
    )

    assert payload.provider == "openai"
    assert payload.model == "gpt-5-nano"
    assert payload.display_name is None


def test_build_payload_carries_document_display_name() -> None:
    payload = ingest_worker._build_payload(
        {
            "run_id": "run-1",
            "kind": "document",
            "document_id": "document-1",
            "workspace_id": "workspace-1",
            "user_id": "user-1",
            "provider": "openai",
            "model": "gpt-5-nano",
            "display_name": "분기 보고서",
        }
    )

    assert payload.display_name == "분기 보고서"


def test_build_chat_payload_defaults_to_partial_selection_mode() -> None:
    payload = ingest_worker._build_payload(
        {
            "run_id": "run-1",
            "kind": "chat_wiki",
            "document_id": "chat-document-1",
            "workspace_id": "workspace-1",
            "user_id": "user-1",
            "provider": "openai",
            "model": "gpt-5-nano",
            "input_markdown": "# Chat Export\n\nQ : 질문\nA : 답변",
            "input_blocks": [
                {"block_id": "session-1:pair-1", "text": "Q : 질문\nA : 답변"}
            ],
        }
    )

    assert payload.selection_mode == "partial"


def test_build_payload_requires_runtime_provider_and_model() -> None:
    with pytest.raises(ValueError, match="requires provider and model"):
        ingest_worker._build_payload(
            {
                "run_id": "run-1",
                "document_id": "document-1",
                "workspace_id": "workspace-1",
                "user_id": "user-1",
            }
        )


def test_build_payload_rejects_chat_command_without_source_blocks() -> None:
    """배포 전환기에 남은 구 형식 command. 재시도해도 못 고치므로 폐기 대상으로 표시한다."""
    with pytest.raises(ingest_worker.UnprocessableIngestCommand, match="payload is invalid"):
        ingest_worker._build_payload(
            {
                "run_id": "run-1",
                "kind": "chat_wiki",
                "document_id": "chatdoc-1",
                "workspace_id": "workspace-1",
                "user_id": "user-1",
                "provider": "openai",
                "model": "gpt-5-nano",
                "selection_mode": "partial",
                # input_markdown·input_blocks 없음
            }
        )


def test_build_payload_rejects_command_without_document_id() -> None:
    """필수 키 누락도 재시도로 못 고치므로 같은 폐기 경로를 탄다."""
    with pytest.raises(ingest_worker.UnprocessableIngestCommand, match="payload is invalid"):
        ingest_worker._build_payload(
            {
                "run_id": "run-1",
                "kind": "document",
                "workspace_id": "workspace-1",
                "user_id": "user-1",
                "provider": "openai",
                "model": "gpt-5-nano",
                # document_id 없음
            }
        )


def test_build_payload_accepts_chat_command_with_source_blocks() -> None:
    payload = ingest_worker._build_payload(
        {
            "run_id": "run-1",
            "kind": "chat_wiki",
            "document_id": "chatdoc-1",
            "workspace_id": "workspace-1",
            "user_id": "user-1",
            "provider": "openai",
            "model": "gpt-5-nano",
            "selection_mode": "partial",
            "input_markdown": "# Chat Export\n\nQ : 질문\nA : 답변",
            "input_blocks": [{"block_id": "session_1:pair_1", "text": "Q : 질문\nA : 답변"}],
        }
    )

    assert payload.input_blocks[0].block_id == "session_1:pair_1"


def test_terminal_failed_result_keeps_failed_event_status() -> None:
    command = {"run_id": "run-1", "kind": "document"}
    result = {"status": "failed", "summary": "pipeline failed"}

    event = ingest_worker._result_event(command, result)

    assert event["event_id"] == "ingest:run-1:failed"
    assert event["status"] == "failed"
    assert event["error"] == "pipeline failed"


def test_post_ingest_command_is_stable_for_redelivery() -> None:
    repository = MagicMock()
    repository.get_run.return_value = {
        "status": "succeeded",
        "manifest": {
            "post_ingest": {
                "cluster_normalized": {"document": {"document_id": "document-1"}},
                "page_ids": ["page-1"],
            }
        },
    }
    command = {
        "run_id": "00000000-0000-0000-0000-000000000001",
        "kind": "document",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "provider": "openai",
        "model": "gpt-5-nano",
    }

    with patch.object(
        ingest_worker,
        "get_pipeline_run_repository",
        return_value=repository,
    ):
        first = ingest_worker._post_ingest_command(command)
        second = ingest_worker._post_ingest_command(command)

    assert first == second
    assert first is not None
    assert first["kind"] == "post_ingest"
    assert first["ingest_run_id"] == command["run_id"]
    assert "cluster_normalized" not in first
    assert "page_ids" not in first


def test_post_ingest_dispatch_failure_is_not_treated_as_finished_ingest() -> None:
    producer = MagicMock()
    producer.send_and_wait = AsyncMock(side_effect=RuntimeError("kafka unavailable"))
    post_command = {
        "run_id": "post-run-1",
        "workspace_id": "workspace-1",
    }

    with (
        patch.object(
            ingest_worker,
            "_post_ingest_command",
            return_value=post_command,
        ),
        patch("app.modules.task_cancellation.infrastructure.postgres_task_journal.register"),
        pytest.raises(ingest_worker.PostIngestDispatchError, match="kafka unavailable"),
    ):
        asyncio.run(
            ingest_worker._dispatch_post_ingest(
                producer,
                {"run_id": "ingest-run-1"},
            )
        )


def test_event_request_excludes_top_level_secrets() -> None:
    command = {
        "run_id": "run-1",
        "kind": "document",
        "provider": "openai",
        "model": "gpt-5-nano",
        "api_key": "api-secret",
        "api_base_url": "https://command.example/v1",
        "base_url": "https://base.example/v1",
        "endpoint": "https://endpoint.example/v1",
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
            "api_base_url": "https://nested-command.example/v1",
            "base_url": "https://nested-base.example/v1",
            "apiEndpoint": "https://nested-endpoint.example/v1",
            "ordinary": {"value": "keep"},
        },
        "items": [
            {"secret": "list-secret", "ordinary": "keep"},
            {"ordinary": {"value": "keep-too"}},
        ],
        "max_tokens": 1024,
    }

    event = ingest_worker._event(command, "succeeded")

    assert event["request"] == {
        "run_id": "run-1",
        "kind": "document",
        "provider": "openai",
        "model": "gpt-5-nano",
        "metadata": {"ordinary": {"value": "keep"}},
        "items": [
            {"ordinary": "keep"},
            {"ordinary": {"value": "keep-too"}},
        ],
        "max_tokens": 1024,
    }


def test_handle_deletes_ai_owned_document_state() -> None:
    command = {
        "kind": "document_deleted",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
    }

    with patch.object(database, "delete_document_wiki_data") as delete:
        ingest_worker._handle(command)

    delete.assert_called_once_with("workspace-1", "document-1")


def test_handle_delete_waits_again_when_concept_lock_times_out() -> None:
    command = {
        "kind": "document_deleted",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
    }

    with patch.object(
        database,
        "delete_document_wiki_data",
        side_effect=[ConceptLockTimeout("busy"), None],
    ) as delete:
        ingest_worker._handle(command)

    assert delete.call_count == 2


def test_handle_delete_gives_up_after_bounded_lock_attempts() -> None:
    command = {
        "kind": "document_deleted",
        "document_id": "document-1",
        "workspace_id": "workspace-1",
    }

    with (
        patch.object(
            database,
            "delete_document_wiki_data",
            side_effect=ConceptLockTimeout("busy"),
        ) as delete,
        pytest.raises(ConceptLockTimeout),
    ):
        ingest_worker._handle(command)

    assert delete.call_count == ingest_worker.DELETE_LOCK_ATTEMPTS


RENAME_COMMAND = {
    "run_id": "run-1",
    "kind": "document_renamed",
    "document_id": "Document-1",
    "workspace_id": "workspace-1",
    "title": "새 이름",
}


def test_handle_rename_updates_source_page_title() -> None:
    with patch.object(database, "rename_source_page") as rename:
        assert ingest_worker._handle(RENAME_COMMAND) == {}

    rename.assert_called_once_with("workspace-1", "Document-1", "새 이름")


def test_handle_rename_rejects_blank_title() -> None:
    with (
        patch.object(database, "rename_source_page") as rename,
        pytest.raises(ingest_worker.UnprocessableIngestCommand),
    ):
        ingest_worker._handle({**RENAME_COMMAND, "title": "  "})

    rename.assert_not_called()


def test_rename_skips_journal_and_rejects_purged_workspace() -> None:
    from app.core.pipeline_control import ScopePurgedError
    from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal

    with (
        patch.object(journal, "connect"),
        patch.object(journal, "workspace_purged", return_value=True),
        patch.object(journal, "execute") as execute,
        patch.object(database, "rename_source_page") as rename,
        pytest.raises(ScopePurgedError),
    ):
        ingest_worker._handle_controlled(RENAME_COMMAND)

    execute.assert_not_called()
    rename.assert_not_called()


def test_rename_event_has_own_kind_and_no_post_ingest() -> None:
    assert ingest_worker._event(RENAME_COMMAND, "succeeded")["kind"] == "document_rename"
    assert ingest_worker._post_ingest_command(RENAME_COMMAND) is None


def test_rename_source_page_matches_slugified_source_page_in_workspace() -> None:
    connection = MagicMock()
    execute = connection.__enter__.return_value.execute
    execute.return_value.rowcount = 0

    with patch.object(database, "connect", return_value=connection):
        # 편입 전 문서는 0행이어도 오류가 아니고, 같은 command를 다시 받아도 같은 UPDATE일 뿐이다.
        assert database.rename_source_page("workspace-1", "Doc_1", "새 이름") == 0
        database.rename_source_page("workspace-1", "Doc_1", "새 이름")

    query, params = execute.call_args.args
    assert "UPDATE wiki_pages" in query and "page_type = 'source'" in query
    assert params == ("새 이름", "workspace-1", "doc-1")
    assert execute.call_count == 2


def test_redelivered_running_ingest_seeks_back_and_waits_instead_of_crashing() -> None:
    from types import SimpleNamespace

    from aiokafka import TopicPartition

    from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal

    handled = [journal.TaskAlreadyExecutingError("busy"), {"status": "succeeded"}]
    message = SimpleNamespace(topic="t", partition=5, offset=7, value={"run_id": "run-1", "kind": "document"})

    async def run_test():
        stop_callbacks = []
        events = []
        consumer = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), commit=AsyncMock(), seek=MagicMock())
        producer = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())

        async def getmany(**kwargs):
            if handled:
                return {"partition": [message]}
            stop_callbacks[0]()
            return {}

        async def send(topic, event, **kwargs):
            events.append(event["status"])

        async def to_thread(function, *args):
            if function is ingest_worker._handle_controlled:
                outcome = handled.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return None

        consumer.getmany = getmany
        producer.send_and_wait = send
        with patch.object(database, "ensure_ai_schema"), \
                patch.object(ingest_worker, "AIOKafkaConsumer", return_value=consumer), \
                patch.object(ingest_worker, "AIOKafkaProducer", return_value=producer), \
                patch.object(ingest_worker, "_dispatch_post_ingest", AsyncMock()), \
                patch.object(asyncio, "to_thread", side_effect=to_thread), \
                patch.object(journal, "REDELIVERY_BACKOFF_SECONDS", 0), \
                patch.object(asyncio.get_running_loop(), "add_signal_handler",
                             side_effect=lambda sig, callback: stop_callbacks.append(callback)):
            await asyncio.wait_for(ingest_worker.consume(), timeout=1)
        return consumer, events

    consumer, events = asyncio.run(run_test())

    consumer.seek.assert_called_once_with(TopicPartition("t", 5), 7)
    assert events == ["succeeded"]
    assert consumer.commit.await_count == 1
