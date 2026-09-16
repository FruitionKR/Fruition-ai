from collections.abc import Callable
from typing import Any

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
            build_manifest: Callable[[], dict[str, Any]],
        ) -> dict[str, Any]:
            nonlocal finished, page_ids
            self._ensure_active(run_id)
            self._ensure_current_source(command)
            with self._repository.concept_write_lock(command.workspace_id, run_id):
                self._ensure_active(run_id)
                self._ensure_current_source(command)
                manifest = build_manifest()
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
                    manifest = finish_after_heavy_phase(lambda: manifest)
            self._embedding_job.start(run_id, page_ids)
            return manifest
        except Exception as exc:
            self._repository.fail(run_id, str(exc))
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
