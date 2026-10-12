import logging
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from psycopg.errors import UniqueViolation

from app.modules.wiki_ingestion.application.models import (
    PipelineRunCommand,
    PipelineRunRegistration,
    WikiMaintenanceConfigurationError,
)
from app.modules.wiki_ingestion.application.ports import (
    PipelineLogReaderPort,
    PipelineRunRepositoryPort,
    PipelineSourceReaderPort,
    WikiMaintenancePort,
)
from app.modules.wiki_ingestion.application.run_pipeline import RunPipelineUseCase
from app.modules.wiki_ingestion.application.restore_wiki_pages import (
    RestoreWikiPagesUseCase,
)
from app.modules.wiki_ingestion.interfaces.http.dependencies import (
    get_pipeline_log_reader,
    pipeline_log_uri,
    get_pipeline_run_repository,
    get_pipeline_run_use_case,
    get_pipeline_source_reader,
    get_restore_wiki_pages_use_case,
    get_wiki_maintenance,
)
from app.modules.wiki_ingestion.interfaces.http.schemas import (
    CHAT_SEMANTIC_PROMPT,
    ChatWikiRunIn,
    IngestOperationRestoreIn,
    LintOperationRestoreIn,
    PipelineRunIn,
    PipelineRunOut,
    ReingestRunIn,
    WikiLintIn,
    WikiLintOut,
    WikiPageLookupIn,
    WikiPageRenameIn,
)
from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)


router = APIRouter()
logger = logging.getLogger("fruition.pipeline")


@router.get("/wiki/graph", tags=["wiki"])
def get_wiki_graph(workspace_id: str) -> dict[str, Any]:
    return database.get_wiki_graph(workspace_id)


@router.post("/wiki/pages/lookup", tags=["wiki"])
def lookup_wiki_pages(payload: WikiPageLookupIn) -> list[dict[str, Any]]:
    return database.lookup_wiki_pages(payload.page_ids, payload.workspace_id)


@router.get("/wiki/pages/{page_id}", tags=["wiki"])
def get_wiki_page(page_id: str, workspace_id: str) -> dict[str, Any]:
    page = database.get_wiki_page(workspace_id, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    return page


@router.patch("/wiki/pages/{page_id}/rename", tags=["wiki"])
def rename_wiki_page(page_id: str, payload: WikiPageRenameIn) -> dict[str, Any]:
    try:
        page = database.rename_wiki_page(
            page_id,
            payload.workspace_id,
            payload.title,
            payload.update_slug,
        )
    except UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="Wiki page slug conflict") from exc
    if page is None:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    return page


@router.get("/wiki/documents/{document_id}/context", tags=["wiki"])
def get_document_wiki_context(
    document_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    return database.get_document_wiki_context(document_id, workspace_id)


@router.delete("/wiki/workspaces/{workspace_id}/documents/{document_id}", tags=["wiki"])
def delete_document_wiki_data(workspace_id: str, document_id: str) -> None:
    database.delete_document_wiki_data(workspace_id, document_id)


@router.get("/wiki/workspaces/{workspace_id}/last-updated", tags=["wiki"])
def get_last_wiki_updated(workspace_id: str) -> dict[str, Any]:
    return {"updated_at": database.get_last_wiki_updated_at(workspace_id)}


@router.post("/wiki/ingest-restore-runs", tags=["wiki"])
def restore_ingest_operation(
    payload: IngestOperationRestoreIn,
    use_case: RestoreWikiPagesUseCase = Depends(
        get_restore_wiki_pages_use_case
    ),
) -> dict[str, Any]:
    return _execute_restore(
        lambda: use_case.execute_ingest(payload.to_command())
    )


@router.post("/wiki/lint-restore-runs", tags=["wiki"])
def restore_lint_operation(
    payload: LintOperationRestoreIn,
    use_case: RestoreWikiPagesUseCase = Depends(
        get_restore_wiki_pages_use_case
    ),
) -> dict[str, Any]:
    return _execute_restore(
        lambda: use_case.execute_lint(payload.to_command())
    )


def _execute_restore(execute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return execute()
    except Exception as exc:
        logger.exception("Wiki restore 처리 중 예상하지 못한 오류가 발생했습니다.")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "internal_server_error",
                "message": "Wiki restore를 처리하지 못했습니다.",
            },
        ) from exc


@router.post("/wiki/maintenance/lint", response_model=WikiLintOut, tags=["wiki"])
def lint_wiki_workspace(
    payload: WikiLintIn,
    maintenance: WikiMaintenancePort = Depends(get_wiki_maintenance),
) -> WikiLintOut:
    try:
        result = maintenance.lint(payload.to_command())
    except WikiMaintenanceConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Wiki maintenance lint 처리 중 예상하지 못한 오류가 발생했습니다.")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "internal_server_error",
                "message": "Wiki maintenance lint를 처리하지 못했습니다.",
            },
        ) from exc
    return WikiLintOut.model_validate(result)


@router.post("/pipeline/runs", response_model=PipelineRunOut, tags=["wiki-ingest"])
def run_pipeline_endpoint(
    payload: PipelineRunIn,
    background_tasks: BackgroundTasks,
    use_case: RunPipelineUseCase = Depends(get_pipeline_run_use_case),
    repository: PipelineRunRepositoryPort = Depends(get_pipeline_run_repository),
    source_reader: PipelineSourceReaderPort = Depends(get_pipeline_source_reader),
) -> PipelineRunOut:
    return _run_pipeline_request(
        payload,
        background_tasks,
        use_case,
        repository,
        source_reader,
    )


@router.post("/pipeline/reingest-runs", response_model=PipelineRunOut, tags=["wiki-ingest"])
def run_reingest_pipeline_endpoint(
    payload: ReingestRunIn,
    background_tasks: BackgroundTasks,
    use_case: RunPipelineUseCase = Depends(get_pipeline_run_use_case),
    repository: PipelineRunRepositoryPort = Depends(get_pipeline_run_repository),
    source_reader: PipelineSourceReaderPort = Depends(get_pipeline_source_reader),
) -> PipelineRunOut:
    return _run_pipeline_request(
        payload,
        background_tasks,
        use_case,
        repository,
        source_reader,
    )


@router.post("/chat-wiki/runs", response_model=PipelineRunOut, tags=["wiki-ingest"])
def run_chat_wiki_endpoint(
    payload: ChatWikiRunIn,
    background_tasks: BackgroundTasks,
    use_case: RunPipelineUseCase = Depends(get_pipeline_run_use_case),
    repository: PipelineRunRepositoryPort = Depends(get_pipeline_run_repository),
    source_reader: PipelineSourceReaderPort = Depends(get_pipeline_source_reader),
) -> PipelineRunOut:
    return _run_pipeline_request(
        payload,
        background_tasks,
        use_case,
        repository,
        source_reader,
    )


@router.get("/pipeline/runs/{run_id}", tags=["wiki-ingest"])
def get_pipeline_run(
    run_id: str,
    repository: PipelineRunRepositoryPort = Depends(get_pipeline_run_repository),
) -> dict[str, Any]:
    try:
        row = repository.get_run(run_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    return row


@router.get(
    "/pipeline/runs/{run_id}/logs",
    response_class=PlainTextResponse,
    tags=["wiki-ingest"],
)
def get_pipeline_logs(
    run_id: str,
    repository: PipelineRunRepositoryPort = Depends(get_pipeline_run_repository),
    log_reader: PipelineLogReaderPort = Depends(get_pipeline_log_reader),
) -> str:
    try:
        row = repository.get_run(run_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="Pipeline run not found")

    return log_reader.read_text(pipeline_log_uri(run_id))


def _run_pipeline_request(
    payload: PipelineRunIn | ReingestRunIn | ChatWikiRunIn,
    background_tasks: BackgroundTasks | None,
    use_case: RunPipelineUseCase,
    repository: PipelineRunRepositoryPort,
    source_reader: PipelineSourceReaderPort,
    run_id: str | None = None,
) -> PipelineRunOut:
    # Kafka worker는 backend가 command에 실어 보낸 run_id를 그대로 쓴다(문서의 runId 대조 유지).
    run_id = run_id or str(uuid.uuid4())
    # 호출자가 out을 지정하지 않으면 run 전용 임시 폴더이므로 실행이 끝나면 지운다(원문이 담긴 디버그 JSON 포함).
    scratch_out = Path("runs") / f"api_{run_id}" if not payload.out else None
    out = Path(payload.out) if payload.out else scratch_out
    log_path = pipeline_log_uri(run_id)
    try:
        document = _load_document(payload.document_id, repository)
    except HTTPException:
        # 문서를 찾지 못해도 실패를 기록할 run은 남긴다.
        _register_run(use_case, payload, run_id, out, payload.user_id)
        raise
    # 공동 편집 문서는 소유자가 아닌 멤버도 편입을 요청한다. user_id는 요청자 귀속 정보이며
    # run 기록과 위키 행에 같은 값을 쓴다. 요청자가 없으면 문서 소유자로 기록한다.
    user_id = payload.user_id or str(document["user_id"])
    _register_run(use_case, payload, run_id, out, user_id)
    workspace_id = str(document["workspace_id"])
    if payload.workspace_id is not None and payload.workspace_id != workspace_id:
        raise HTTPException(
            status_code=409,
            detail="Pipeline command workspace does not match the document.",
        )
    input_name = payload.input_name or "inline.md"
    reingest_source_context = None
    reingest_source_blocks: list[dict[str, Any]] = []

    if isinstance(payload, ChatWikiRunIn):
        input_markdown, input_source, input_name = _resolve_chat_wiki_input(payload)
    elif isinstance(payload, ReingestRunIn):
        reingest_source_context = _require_reingest_source_context(
            payload.document_id,
            workspace_id,
            repository,
        )
        reingest_source_blocks = _load_existing_source_blocks(
            payload.document_id,
            repository,
        )
        input_markdown = payload.input_markdown
        input_name = payload.input_name or str(
            document.get("filename") or f"{_safe_name(payload.document_id)}.md"
        )
        input_source = f"inline:{input_name}"
    else:
        input_markdown, input_source, input_name = _load_stored_document_input(
            document,
            source_reader,
        )

    command = _build_pipeline_command(
        payload,
        run_id,
        input_markdown,
        input_name,
        out,
        log_path,
        payload.document_id,
        user_id,
        workspace_id,
        repository,
        reingest_source_context=reingest_source_context,
        reingest_source_blocks=reingest_source_blocks,
        display_name=_source_display_name(payload, document),
    )

    if not payload.wait:
        background_tasks.add_task(
            _execute_pipeline_run,
            run_id,
            command,
            use_case,
            scratch_out,
        )
        return PipelineRunOut(
            run_id=run_id,
            status="running",
            manifest=None,
            output_dir=str(out),
            log_path=str(log_path),
        )

    try:
        manifest = use_case.execute(run_id, command)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        _remove_scratch_out(scratch_out)

    run_status = "succeeded"
    stored_run = repository.get_run(run_id)
    if isinstance(stored_run, dict) and stored_run.get("status"):
        run_status = str(stored_run["status"])

    return PipelineRunOut(
        run_id=run_id,
        status=run_status,
        manifest=manifest,
        output_dir=str(out),
        log_path=str(log_path),
    )


def _register_run(
    use_case: RunPipelineUseCase,
    payload: PipelineRunIn | ReingestRunIn | ChatWikiRunIn,
    run_id: str,
    out: Path,
    user_id: str | None,
) -> None:
    try:
        use_case.register(
            PipelineRunRegistration(
                run_id=run_id,
                document_id=payload.document_id,
                user_id=user_id,
                workspace_id=payload.workspace_id,
                input_source=f"document:{payload.document_id}",
                output_dir=str(out),
                mode=payload.mode,
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _build_pipeline_command(
    payload: PipelineRunIn | ReingestRunIn | ChatWikiRunIn,
    run_id: str,
    input_markdown: str | None,
    input_name: str,
    out: Path,
    log_path: str | Path,
    source_document_id: str | None,
    user_id: str,
    workspace_id: str,
    repository: PipelineRunRepositoryPort,
    *,
    reingest_source_context: dict[str, Any] | None = None,
    reingest_source_blocks: list[dict[str, Any]] | None = None,
    display_name: str | None = None,
) -> PipelineRunCommand:
    existing_concept_index = _load_existing_concept_index_for_run(
        workspace_id,
        repository,
    )
    existing_source_context = reingest_source_context
    return PipelineRunCommand(
        run_id=run_id,
        operation_id=payload.operation_id,
        source_document_id=source_document_id,
        source_revision=payload.source_revision,
        source_content_hash=payload.source_content_hash,
        selection_mode=getattr(payload, "selection_mode", None),
        reingest=isinstance(payload, ReingestRunIn),
        input=input_name,
        input_markdown=input_markdown,
        input_blocks=[
            {"block_id": block.block_id, "text": block.text}
            for block in getattr(payload, "input_blocks", None) or []
        ],
        input_name=input_name,
        display_name=display_name,
        out=str(out),
        mode=payload.mode,
        provider=payload.provider,
        source_page_mode=payload.source_page_mode,
        concept_page_mode=payload.concept_page_mode,
        max_packet_chars=payload.max_packet_chars,
        overlap_blocks=payload.overlap_blocks,
        model=payload.model,
        system_prompt=(
            payload.chat_system_prompt
            if isinstance(payload, ChatWikiRunIn)
            else payload.system_prompt
        ),
        concept_system_prompt=payload.concept_system_prompt,
        concept_resolution_system_prompt=payload.concept_resolution_system_prompt,
        wiki_evaluator_system_prompt=payload.wiki_evaluator_system_prompt,
        existing_wiki_dir=payload.existing_wiki_dir,
        existing_concept_index=existing_concept_index,
        existing_source_artifact=(existing_source_context or {}).get("artifact"),
        existing_source_markdown=(existing_source_context or {}).get(
            "source_markdown"
        ),
        existing_source_blocks=list(reingest_source_blocks or []),
        wiki_evaluation_loop=payload.wiki_evaluation_loop,
        max_eval_attempts=payload.max_eval_attempts,
        save_debug_json=payload.save_debug_json,
        log_path=str(log_path),
        log_callback_url=payload.log_callback_url,
        user_id=user_id,
        workspace_id=workspace_id,
    )


def _load_existing_concept_index_for_run(
    workspace_id: str,
    repository: PipelineRunRepositoryPort,
) -> list[dict[str, Any]]:
    try:
        return repository.list_active_concept_index(workspace_id)
    except Exception:
        logger.exception("failed to load existing concept index for pipeline run")
        return []


def _require_reingest_source_context(
    document_id: str,
    workspace_id: str,
    repository: PipelineRunRepositoryPort,
) -> dict[str, Any]:
    try:
        context = repository.latest_source_page_context(
            document_id,
            workspace_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not context:
        raise HTTPException(
            status_code=409,
            detail="재편입하려면 기존 활성 source page가 필요합니다.",
        )
    return context


def _load_existing_source_blocks(
    document_id: str,
    repository: PipelineRunRepositoryPort,
) -> list[dict[str, Any]]:
    try:
        return repository.list_source_blocks(document_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _source_display_name(
    payload: PipelineRunIn | ReingestRunIn | ChatWikiRunIn,
    document: dict[str, Any],
) -> str | None:
    """소스 페이지 제목. 채팅 export는 document가 생성 제목으로 파일 이름을 정하므로 넘기지 않는다."""
    if isinstance(payload, ChatWikiRunIn):
        return None
    if payload.display_name:
        return payload.display_name
    filename = str(document.get("filename") or "")
    # document `Document.displayNameOf`와 같은 규칙: 마지막 확장자만 떼고, 점으로 시작하면 그대로 둔다.
    extension_index = filename.rfind(".")
    return (filename[:extension_index] if extension_index > 0 else filename) or None


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._")
    return cleaned or "document"


def _load_document(
    document_id: str,
    repository: PipelineRunRepositoryPort,
) -> dict[str, Any]:
    try:
        document = repository.get_document(document_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


def _load_document_markdown(
    document: dict[str, Any],
    source_reader: PipelineSourceReaderPort,
) -> tuple[str, str, str]:
    source_uri = document.get("source_uri")
    extracted_text_uri = document.get("extracted_text_uri")
    mime_type = (document.get("mime_type") or "").lower()

    if extracted_text_uri:
        object_uri = extracted_text_uri
    elif mime_type in {"text/markdown", "text/x-markdown", "text/plain"} or str(
        document.get("filename", "")
    ).lower().endswith(".md"):
        object_uri = source_uri
    else:
        raise HTTPException(
            status_code=409,
            detail="Document needs extracted_text_uri before pipeline processing. Convert the source file to Markdown/text first.",
        )

    if not object_uri:
        raise HTTPException(
            status_code=409,
            detail="Document has no source_uri or extracted_text_uri",
        )

    try:
        markdown = source_reader.read_text(str(object_uri))
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to read document object from storage: {exc}",
        ) from exc

    return (
        markdown,
        str(object_uri),
        str(document.get("filename") or f"{_safe_name(str(document['id']))}.md"),
    )


def _load_stored_document_input(
    document: dict[str, Any],
    source_reader: PipelineSourceReaderPort,
) -> tuple[str, str, str]:
    input_markdown, object_uri, input_name = _load_document_markdown(
        document,
        source_reader,
    )
    return input_markdown, f"storage:{object_uri}", input_name


def _resolve_chat_wiki_input(payload: ChatWikiRunIn) -> tuple[str, str, str]:
    """채팅은 항상 inline이다. 블록은 payload가 들고 오므로 storage 원문을 다시 읽지 않는다."""
    input_name = payload.input_name or "chat.md"
    return payload.input_markdown, f"inline:{input_name}", input_name


def _remove_scratch_out(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def _execute_pipeline_run(
    run_id: str,
    command: PipelineRunCommand,
    use_case: RunPipelineUseCase,
    scratch_out: Path | None = None,
) -> None:
    try:
        use_case.execute(run_id, command)
    except Exception as exc:
        logger.error("ERROR: pipeline run failed run_id=%s error=%s", run_id, exc)
    finally:
        _remove_scratch_out(scratch_out)
