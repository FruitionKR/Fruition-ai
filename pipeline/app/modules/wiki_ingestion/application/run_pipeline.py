import logging
from collections.abc import Callable
from typing import Any

from app.core.failure_codes import failure_code
from app.core.pipeline_control import PipelineRunCancelledError, task_cancellation_scope
from app.modules.wiki_ingestion.application.models import (
    PipelineRunCommand,
    PipelineRunRegistration,
)
from app.modules.wiki_ingestion.application.ports import (
    PipelineRunnerPort,
    PipelineRunRepositoryPort,
    WikiEmbeddingJobPort,
)

logger = logging.getLogger(__name__)


class RunPipelineUseCase:
    def __init__(
        self,
        runner: PipelineRunnerPort,
        repository: PipelineRunRepositoryPort,
        embedding_job: WikiEmbeddingJobPort,
    ) -> None:
        self._runner = runner
        self._repository = repository
        self._embedding_job = embedding_job

    def register(self, registration: PipelineRunRegistration) -> None:
        self._repository.create(
            registration.run_id,
            registration.document_id,
            registration.user_id,
            registration.workspace_id,
            registration.input_source,
            registration.output_dir,
            registration.mode,
        )

    def execute(self, run_id: str, command: PipelineRunCommand) -> dict[str, Any]:
        page_ids: list[str] = []
        finished = False

        def finish_after_heavy_phase(
            build_manifest: Callable[[list[dict[str, Any]]], dict[str, Any]],
        ) -> dict[str, Any]:
            nonlocal finished, page_ids
            self._ensure_active(run_id)
            self._ensure_current_source(command)
            with self._repository.concept_write_lock(command.workspace_id, run_id):
                self._ensure_active(run_id)
                self._ensure_current_source(command)
                # 개념 판정은 잠금을 잡은 뒤의 개념 목록으로 한다. 동시에 편입한 문서가 먼저 저장한 개념도 본다.
                manifest = build_manifest(
                    self._repository.list_active_concept_index(command.workspace_id, use_cache=False)
                )
                self._ensure_active(run_id)
                self._ensure_current_source(command)
                page_ids = self._repository.finish(
                    run_id,
                    manifest,
                    command.source_content_hash,
                )
                finished = True
            return manifest

        try:
            self._ensure_active(run_id)
            runner_kwargs: dict[str, Any] = {
                "progress_callback": lambda: self._repository.touch(run_id),
            }
            if command.source_document_id is not None:
                runner_kwargs["finalization_callback"] = finish_after_heavy_phase
            with task_cancellation_scope(lambda: self._repository.touch(run_id)):
                manifest = self._runner.run(command, **runner_kwargs)
            if not finished:
                if command.source_document_id is None:
                    self._ensure_active(run_id)
                    self._ensure_current_source(command)
                    page_ids = self._repository.finish(
                        run_id,
                        manifest,
                        command.source_content_hash,
                    )
                    finished = True
                else:
                    manifest = finish_after_heavy_phase(lambda _concepts: manifest)
            self._embedding_job.start(run_id, page_ids)
            return manifest
        except Exception as exc:
            logger.warning("[pipeline 실행 실패] run_id=%s", run_id, exc_info=True)
            self._repository.fail(run_id, failure_code(exc))
            raise

    def _ensure_current_source(self, command: PipelineRunCommand) -> None:
        if command.source_document_id is None or (
            command.source_revision is None and command.source_content_hash is None
        ):
            return
        current = self._repository.get_document(command.source_document_id)
        revision_changed = (
            command.source_revision is not None
            and int((current or {}).get("source_revision") or -1) != command.source_revision
        )
        hash_changed = (
            command.source_content_hash is not None
            and str((current or {}).get("source_content_hash")) != command.source_content_hash
        )
        if current is None or revision_changed or hash_changed:
            raise ValueError("ingest source revision is stale")

    def _ensure_active(self, run_id: str) -> None:
        if self._repository.touch(run_id) is False:
            raise PipelineRunCancelledError(
                "Pipeline run cancelled because its document or workspace is inactive."
            )
