import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import Mock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api
from app.modules.wiki_ingestion.application.run_pipeline import RunPipelineUseCase
from app.modules.wiki_ingestion.interfaces.http import routes as pipeline_routes
from app.modules.wiki_ingestion.interfaces.http.schemas import (
    CHAT_SEMANTIC_PROMPT,
)


def _repository(
    *,
    document: dict | None = None,
    source_context: dict | None = None,
) -> Mock:
    repository = Mock()
    repository.get_document.return_value = document
    repository.latest_source_page_context.return_value = source_context
    repository.list_source_blocks.return_value = []
    repository.list_active_concept_index.return_value = []
    return repository


def _source_reader(markdown: str = "# Document") -> Mock:
    reader = Mock()
    reader.read_text.return_value = markdown
    return reader


def _use_case() -> Mock:
    use_case = Mock(spec=RunPipelineUseCase)
    use_case.execute.return_value = {"manifest": "value"}
    return use_case


def _internal_token_headers() -> dict[str, str]:
    return {"X-Internal-Token": os.environ["INTERNAL_CALLBACK_TOKEN"]}


def _pipeline_run_in(**data: object):
    data.setdefault("provider", "openai")
    data.setdefault("model", "gpt-5-nano")
    return api.PipelineRunIn(**data)


CHAT_MARKDOWN = "# Chat Export\n\nQ : 새 질문\nA : 새 답변"
CHAT_BLOCKS = [{"block_id": "session_1:pair_2", "text": "Q : 새 질문\nA : 새 답변"}]


def _chat_wiki_run_in(**data: object):
    data.setdefault("provider", "openai")
    data.setdefault("model", "gpt-5-nano")
    data.setdefault("selection_mode", "partial")
    data.setdefault("input_markdown", CHAT_MARKDOWN)
    data.setdefault("input_blocks", CHAT_BLOCKS)
    return api.ChatWikiRunIn(**data)


@contextmanager
def _pipeline_client(
    *,
    use_case: Mock | None = None,
    repository: Mock | None = None,
    source_reader: Mock | None = None,
    log_reader: Mock | None = None,
) -> Iterator[TestClient]:
    previous_overrides = dict(api.app.dependency_overrides)
    if use_case is not None:
        api.app.dependency_overrides[pipeline_routes.get_pipeline_run_use_case] = (
            lambda: use_case
        )
    if repository is not None:
        api.app.dependency_overrides[pipeline_routes.get_pipeline_run_repository] = (
            lambda: repository
        )
    if source_reader is not None:
        api.app.dependency_overrides[pipeline_routes.get_pipeline_source_reader] = (
            lambda: source_reader
        )
    if log_reader is not None:
        api.app.dependency_overrides[pipeline_routes.get_pipeline_log_reader] = (
            lambda: log_reader
        )
    try:
        with (
            patch.dict("os.environ", {"INTERNAL_CALLBACK_TOKEN": "test-internal-token"}),
            patch.object(api.database, "ensure_ai_schema"),
            TestClient(api.app) as client,
        ):
            client.headers.update(_internal_token_headers())
            yield client
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(previous_overrides)


def test_pipeline_lifespan_verifies_ai_schema() -> None:
    with (
        patch.object(api.database, "ensure_ai_schema") as ensure_ai_schema,
        TestClient(api.app),
    ):
        pass

    ensure_ai_schema.assert_called_once_with()


def test_pipeline_run_rejects_chat_selection_mode() -> None:
    try:
        _pipeline_run_in(document_id="chat_document_1", selection_mode="full")
    except ValidationError as exc:
        assert "selection_mode" in str(exc)
    else:
        raise AssertionError("document pipeline request should reject chat selection_mode")


def test_pipeline_run_requires_document_id() -> None:
    try:
        _pipeline_run_in()
    except ValidationError as exc:
        assert "document_id" in str(exc)
    else:
        raise AssertionError("document pipeline request should require document_id")


def test_reingest_run_accepts_empty_markdown() -> None:
    payload = api.ReingestRunIn(
        document_id="document_1",
        input_markdown="",
        provider="openai",
        model="gpt-5-nano",
    )

    assert payload.input_markdown == ""


def test_pipeline_run_rejects_direct_input() -> None:
    for field in ("input_markdown", "input_path"):
        try:
            _pipeline_run_in(document_id="document_1", **{field: "direct-input"})
        except ValidationError as exc:
            assert field in str(exc)
        else:
            raise AssertionError(f"document pipeline request should reject {field}")


def test_pipeline_run_accepts_legacy_document_scope_fields() -> None:
    payload = _pipeline_run_in(
        document_id="document_1",
        user_id="request-user",
        workspace_id="request-workspace",
    )

    assert payload.user_id == "request-user"
    assert payload.workspace_id == "request-workspace"
    assert payload.wiki_evaluation_loop is True


def test_pipeline_command_includes_operation_id() -> None:
    repository = _repository()
    payload = _pipeline_run_in(
        document_id="document_1",
        operation_id="op_1",
    )

    command = pipeline_routes._build_pipeline_command(
        payload,
        run_id="run_1",
        input_markdown="# Document",
        input_name="document.md",
        out=Path("runs/test"),
        log_path=Path("runs/test/pipeline.log"),
        source_document_id="document_1",
        user_id="user_1",
        workspace_id="workspace_1",
        repository=repository,
    )

    assert command.operation_id == "op_1"
    assert command.provider == "openai"
    assert command.model == "gpt-5-nano"


def test_chat_wiki_run_accepts_partial_selection_mode() -> None:
    payload = _chat_wiki_run_in(document_id="chat_document_1")

    assert payload.document_id == "chat_document_1"
    assert payload.selection_mode == "partial"
    assert payload.wiki_evaluation_loop is True


def test_chat_wiki_run_carries_provenance_in_blocks_not_markdown() -> None:
    payload = _chat_wiki_run_in(document_id="chat_document_1")

    assert payload.input_markdown.startswith("# Chat Export")
    assert "session_1:pair_2" not in payload.input_markdown
    assert payload.input_blocks[0].block_id == "session_1:pair_2"
    assert "session_1" not in payload.input_blocks[0].text


def test_chat_wiki_run_requires_input_blocks() -> None:
    try:
        api.ChatWikiRunIn(
            provider="openai",
            model="gpt-5-nano",
            document_id="chat_document_1",
            selection_mode="partial",
            input_markdown=CHAT_MARKDOWN,
            input_blocks=[],
        )
    except ValidationError as exc:
        assert "input_blocks" in str(exc)
    else:
        raise AssertionError("chat wiki run should require at least one source block")


def test_chat_wiki_run_requires_input_markdown() -> None:
    try:
        api.ChatWikiRunIn(
            provider="openai",
            model="gpt-5-nano",
            document_id="chat_document_1",
            selection_mode="partial",
            input_blocks=CHAT_BLOCKS,
        )
    except ValidationError as exc:
        assert "input_markdown" in str(exc)
    else:
        raise AssertionError("chat wiki run should require input_markdown")


@pytest.mark.parametrize("selection_mode", ["full", "append"])
def test_chat_wiki_run_rejects_unsupported_selection_mode(selection_mode: str) -> None:
    try:
        _chat_wiki_run_in(document_id="chat_document_1", selection_mode=selection_mode)
    except ValidationError as exc:
        assert "selection_mode" in str(exc)
    else:
        raise AssertionError("selection_mode validation should reject unknown values")


def test_chat_wiki_run_requires_document_id() -> None:
    try:
        _chat_wiki_run_in()
    except ValidationError as exc:
        assert "document_id" in str(exc)
    else:
        raise AssertionError("chat wiki run should require document_id")


def test_pipeline_command_includes_selection_mode() -> None:
    repository = _repository()
    payload = _chat_wiki_run_in(document_id="chat_document_1", selection_mode="partial")

    command = pipeline_routes._build_pipeline_command(
        payload,
        run_id="run_1",
        input_markdown="# Chat Export",
        input_name="chat.md",
        out=Path("runs/test"),
        log_path=Path("runs/test/pipeline.log"),
        source_document_id="chat_document_1",
        user_id="user_1",
        workspace_id="workspace_1",
        repository=repository,
    )

    assert command.selection_mode == "partial"
    assert command.system_prompt == CHAT_SEMANTIC_PROMPT
    assert command.input_blocks == CHAT_BLOCKS


def test_pipeline_command_does_not_load_existing_source_context_for_partial() -> None:
    repository = _repository(
        source_context={
            "artifact": {"document_id": "chat_document_1"},
            "source_markdown": "# Existing Source",
        },
    )
    payload = _chat_wiki_run_in(document_id="chat_document_1")

    command = pipeline_routes._build_pipeline_command(
        payload,
        run_id="run_1",
        input_markdown="# Chat Export",
        input_name="chat.md",
        out=Path("runs/test"),
        log_path=Path("runs/test/pipeline.log"),
        source_document_id="chat_document_1",
        user_id="user_1",
        workspace_id="workspace_1",
        repository=repository,
    )

    assert command.existing_source_artifact is None
    assert command.existing_source_markdown is None
    assert command.system_prompt == CHAT_SEMANTIC_PROMPT
    repository.latest_source_page_context.assert_not_called()


def test_pipeline_endpoint_rejects_selection_mode() -> None:
    client = TestClient(
        api.app,
        headers={"X-Internal-Token": "test-internal-token"},
    )

    response = client.post(
        "/pipeline/runs",
        json={"document_id": "chat_document_1", "selection_mode": "full"},
        headers=_internal_token_headers(),
    )

    assert response.status_code == 422


def test_chat_wiki_endpoint_requires_selection_mode() -> None:
    client = TestClient(
        api.app,
        headers={"X-Internal-Token": "test-internal-token"},
    )

    response = client.post(
        "/chat-wiki/runs",
        json={"document_id": "chat_document_1"},
        headers=_internal_token_headers(),
    )

    assert response.status_code == 422


def test_pipeline_endpoint_runs_stored_document_in_background() -> None:
    repository = _repository(
        document={
            "id": "document_1",
            "user_id": "user_1",
            "workspace_id": "workspace_1",
            "source_uri": "documents/document_1.md",
            "extracted_text_uri": None,
            "mime_type": "text/markdown",
            "filename": "document.md",
        },
    )
    use_case = _use_case()
    source_reader = _source_reader("# Stored Document")

    with _pipeline_client(
        use_case=use_case,
        repository=repository,
        source_reader=source_reader,
    ) as client:
        response = client.post(
            "/pipeline/runs",
            json={"document_id": "document_1", "provider": "openai", "model": "gpt-5-nano"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert [item[0] for item in use_case.mock_calls] == ["register", "execute"]
    registration = use_case.register.call_args.args[0]
    command = use_case.execute.call_args.args[1]
    assert registration.input_source == "document:document_1"
    assert command.input_markdown == "# Stored Document"
    assert command.source_document_id == "document_1"


def test_reingest_endpoint_uses_inline_markdown_and_previous_source() -> None:
    repository = _repository(
        document={
            "id": "document_1",
            "user_id": "user_1",
            "workspace_id": "workspace_1",
            "filename": "document.md",
        },
        source_context={
            "artifact": {"document_id": "document_1"},
            "source_markdown": "# 기존 문서",
        },
    )
    repository.list_source_blocks.return_value = [
        {"document_id": "document_1", "block_id": "B0001", "text": "기존 문서"}
    ]
    use_case = _use_case()
    source_reader = _source_reader()

    with _pipeline_client(
        use_case=use_case,
        repository=repository,
        source_reader=source_reader,
    ) as client:
        response = client.post(
            "/pipeline/reingest-runs",
            json={
                "document_id": "document_1",
                "input_markdown": "# 수정 문서",
                "provider": "openai",
                "model": "gpt-5-nano",
            },
        )

    assert response.status_code == 200
    registration = use_case.register.call_args.args[0]
    command = use_case.execute.call_args.args[1]
    assert registration.input_source == "document:document_1"
    assert command.reingest is True
    assert command.input_markdown == "# 수정 문서"
    assert command.existing_source_artifact == {"document_id": "document_1"}
    assert command.existing_source_blocks == repository.list_source_blocks.return_value
    source_reader.read_text.assert_not_called()


def test_reingest_endpoint_rejects_document_without_existing_source_page() -> None:
    repository = _repository(
        document={
            "id": "document_1",
            "user_id": "user_1",
            "workspace_id": "workspace_1",
            "filename": "document.md",
        },
    )

    with _pipeline_client(
        use_case=_use_case(),
        repository=repository,
        source_reader=_source_reader(),
    ) as client:
        response = client.post(
            "/pipeline/reingest-runs",
            json={
                "document_id": "document_1",
                "input_markdown": "# 수정 문서",
                "provider": "openai",
                "model": "gpt-5-nano",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "재편입하려면 기존 활성 source page가 필요합니다."
    repository.list_source_blocks.assert_not_called()


def test_pipeline_endpoint_waits_for_synchronous_result() -> None:
    repository = _repository(
        document={
            "id": "document_1",
            "user_id": "user_1",
            "workspace_id": "workspace_1",
            "source_uri": "documents/document_1.md",
            "extracted_text_uri": None,
            "mime_type": "text/markdown",
            "filename": "document.md",
        },
    )
    use_case = _use_case()
    source_reader = _source_reader("# Stored Document")

    with _pipeline_client(
        use_case=use_case,
        repository=repository,
        source_reader=source_reader,
    ) as client:
        response = client.post(
            "/pipeline/runs",
            json={
                "document_id": "document_1",
                "wait": True,
                "provider": "openai",
                "model": "gpt-5-nano",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["manifest"] == {"manifest": "value"}
    assert [item[0] for item in use_case.mock_calls] == ["register", "execute"]


def test_pipeline_run_status_uses_repository_dependency() -> None:
    repository = _repository()
    repository.get_run.return_value = {
        "id": "run-1",
        "status": "succeeded",
        "output_dir": "runs/run-1",
    }

    with _pipeline_client(repository=repository) as client:
        response = client.get("/pipeline/runs/run-1")

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    repository.get_run.assert_called_once_with("run-1")


def test_pipeline_run_logs_use_durable_run_key() -> None:
    repository = _repository()
    repository.get_run.return_value = {
        "id": "run-1",
        "status": "succeeded",
        "output_dir": "runs/run-1",
        "manifest": {"pipeline_log": "runs/custom/pipeline.log"},
    }
    log_reader = Mock()
    log_reader.read_text.return_value = "파이프라인 완료"

    with _pipeline_client(repository=repository, log_reader=log_reader) as client:
        response = client.get("/pipeline/runs/run-1/logs")

    assert response.status_code == 200
    assert response.text == "파이프라인 완료"
    log_reader.read_text.assert_called_once_with(pipeline_routes.pipeline_log_uri("run-1"))


def test_chat_wiki_inline_markdown_uses_document_id_as_source_key() -> None:
    repository = _repository(
        document={
            "id": "chat_document_1",
            "user_id": "user_1",
            "workspace_id": "workspace_1",
        },
        source_context={
            "artifact": {"document_id": "chat_document_1"},
            "source_markdown": "# Existing Source",
        },
    )
    use_case = _use_case()
    source_reader = _source_reader()

    payload = _chat_wiki_run_in(
        document_id="chat_document_1",
        selection_mode="partial",
        input_markdown="# Chat Export\n\n[session_1:pair_2]Q : 새 질문\nA : 새 답변",
        input_name="filtered-chat.md",
        user_id="user_1",
        workspace_id="workspace_1",
    )

    response = pipeline_routes._run_pipeline_request(
        payload,
        BackgroundTasks(),
        use_case,
        repository,
        source_reader,
    )

    assert response.status == "running"
    registration = use_case.register.call_args.args[0]
    assert registration.document_id == "chat_document_1"
    assert registration.input_source == "document:chat_document_1"
    source_reader.read_text.assert_not_called()


def test_pipeline_command_rejects_actor_mismatch_after_registering_run() -> None:
    repository = _repository(
        document={
            "id": "document_1",
            "user_id": "user_1",
            "workspace_id": "workspace_1",
        },
    )
    use_case = _use_case()
    payload = _pipeline_run_in(
        document_id="document_1",
        user_id="other-user",
        workspace_id="workspace_1",
    )

    with pytest.raises(HTTPException) as exc_info:
        pipeline_routes._run_pipeline_request(
            payload,
            BackgroundTasks(),
            use_case,
            repository,
            _source_reader(),
            run_id="run-1",
        )

    assert exc_info.value.status_code == 409
    assert use_case.register.call_args.args[0].run_id == "run-1"


def test_chat_wiki_inline_markdown_requires_existing_document() -> None:
    repository = _repository(
        source_context={
            "artifact": {"document_id": "missing_document"},
            "source_markdown": "# Existing Source",
        },
    )
    payload = _chat_wiki_run_in(
        document_id="missing_document",
        selection_mode="partial",
        input_markdown="# Chat Export\n\n[session_1:pair_2]Q : 새 질문\nA : 새 답변",
    )

    try:
        pipeline_routes._run_pipeline_request(
            payload,
            BackgroundTasks(),
            _use_case(),
            repository,
            _source_reader(),
        )
    except HTTPException as exc:
        assert exc.status_code == 404
        assert exc.detail == "Document not found"
    else:
        raise AssertionError("chat wiki inline input should require an existing backend document")


def test_agent_service_token_is_required_when_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_INTERNAL_TOKEN", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        api.require_agent_service_token("token")

    assert exc_info.value.status_code == 503


def test_agent_service_token_rejects_invalid_value(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_INTERNAL_TOKEN", "expected-token")

    with pytest.raises(HTTPException) as exc_info:
        api.require_agent_service_token("wrong-token")

    assert exc_info.value.status_code == 401


def test_agent_service_token_accepts_matching_value(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_INTERNAL_TOKEN", "expected-token")

    api.require_agent_service_token("expected-token")


def test_internal_token_is_required_when_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("INTERNAL_CALLBACK_TOKEN", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        api.require_internal_token("token")

    assert exc_info.value.status_code == 503


def test_internal_token_rejects_invalid_value(monkeypatch) -> None:
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "expected-token")

    with pytest.raises(HTTPException) as exc_info:
        api.require_internal_token("wrong-token")

    assert exc_info.value.status_code == 401


def test_internal_token_accepts_matching_value(monkeypatch) -> None:
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "expected-token")

    api.require_internal_token("expected-token")


def test_pipeline_route_rejects_missing_internal_token(monkeypatch) -> None:
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "expected-token")

    response = TestClient(api.app).post(
        "/pipeline/runs",
        json={"document_id": "document_1"},
    )

    assert response.status_code == 401


def test_metrics_route_is_open_without_internal_token(monkeypatch) -> None:
    """/metrics는 Prometheus가 인증 없이 긁어야 한다.

    계측 등록이 INTERNAL_TOKEN_ROUTE_PATTERNS보다 앞으로 옮겨지면 /metrics가 보호 목록에
    잘못 들어가 401이 되고 수집이 통째로 끊긴다. 그 회귀를 막는다.
    """
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "expected-token")

    response = TestClient(api.app).get("/metrics")

    assert response.status_code == 200


def test_pipeline_route_authenticates_before_parsing_body(monkeypatch) -> None:
    monkeypatch.setenv("INTERNAL_CALLBACK_TOKEN", "expected-token")

    response = TestClient(api.app).post(
        "/pipeline/runs",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401


def test_health_does_not_require_internal_token(monkeypatch) -> None:
    monkeypatch.delenv("INTERNAL_CALLBACK_TOKEN", raising=False)

    response = TestClient(api.app).get("/health")

    assert response.status_code == 200


@pytest.mark.parametrize("status", ["running", "succeeded", "failed", "cancelled"])
def test_pipeline_log_survives_worker_directory_removal(tmp_path, monkeypatch, status):
    import shutil
    from io import BytesIO
    from app.modules.wiki_generation.infrastructure.pipeline_log import PipelineLog
    from app.modules.wiki_ingestion.infrastructure import object_storage
    from app.modules.wiki_ingestion.infrastructure.pipeline_run_adapters import ObjectStoragePipelineLogReader

    objects = {}
    client = Mock()
    client.put_object.side_effect = lambda bucket, key, data, **kwargs: objects.__setitem__((bucket, key), data.read())
    def get_object(bucket, key):
        response = BytesIO(objects[bucket, key])
        response.release_conn = lambda: None
        return response
    client.get_object.side_effect = get_object
    monkeypatch.setattr(object_storage, "client", lambda: client)
    worker = tmp_path / "worker"
    worker.mkdir()
    monkeypatch.chdir(worker)
    log = PipelineLog(pipeline_routes.pipeline_log_uri("run-1"), run_id="run-1")
    log.emit("진행", "다른 Pod에서도 조회")
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    monkeypatch.chdir(api_dir)
    shutil.rmtree(worker)
    repository = _repository()
    repository.get_run.return_value = {"id": "run-1", "status": status, "output_dir": str(worker), "manifest": None}
    with _pipeline_client(repository=repository, log_reader=ObjectStoragePipelineLogReader()) as api_client:
        response = api_client.get("/pipeline/runs/run-1/logs")
        state = api_client.get("/pipeline/runs/run-1")
    assert response.status_code == 200
    assert "다른 Pod에서도 조회" in response.text
    assert state.json()["status"] == status
    assert not list(api_dir.iterdir())
