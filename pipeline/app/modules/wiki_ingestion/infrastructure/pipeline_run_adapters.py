from collections.abc import Callable
from typing import Any, ContextManager

from app.modules.wiki_ingestion.application.models import PipelineRunCommand
from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)
from app.modules.wiki_ingestion.infrastructure.object_storage import read_text_object
from minio.error import S3Error
from run_lab import run_pipeline


class RunLabPipelineRunner:
    def run(
        self,
        command: PipelineRunCommand,
        progress_callback: Callable[[], bool | None] | None = None,
        finalization_callback: Callable[
            [Callable[[list[dict[str, Any]]], dict[str, Any]]], dict[str, Any]
        ]
        | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"progress_callback": progress_callback}
        if finalization_callback is not None:
            kwargs["finalization_callback"] = finalization_callback
        return run_pipeline(command, **kwargs)


class PostgresPipelineRunRepository:
    def concept_write_lock(
        self,
        workspace_id: str,
        run_id: str,
    ) -> ContextManager[None]:
        # 편입 반영 단계는 개념 판정까지 잠금 안에서 하므로, 동시에 끝난 편입들이 차례를 오래 기다릴 수 있다.
        # 10분이면 30분인 ingest worker max_poll_interval 안에서 문서 편입과 대기가 끝난다.
        return database.concept_write_lock(workspace_id, run_id, lock_timeout="10min")

    def create(
        self,
        run_id: str,
        document_id: str | None,
        user_id: str | None,
        workspace_id: str | None,
        input_source: str,
        output_dir: str,
        mode: str,
    ) -> None:
        database.create_pipeline_run(
            run_id,
            document_id,
            user_id,
            workspace_id,
            input_source,
            output_dir,
            mode,
        )

    def finish(
        self,
        run_id: str,
        manifest: dict[str, Any],
        expected_source_hash: str | None = None,
    ) -> list[str]:
        return database.finish_pipeline_run(run_id, manifest, expected_source_hash)

    def fail(self, run_id: str, error: str) -> None:
        database.fail_pipeline_run(run_id, error)

    def touch(self, run_id: str) -> bool:
        return database.touch_pipeline_run(run_id)

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        return database.get_document(document_id)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return database.get_pipeline_run(run_id)

    def list_active_concept_index(
        self,
        workspace_id: str,
        *,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        return database.list_active_concept_index(workspace_id, use_cache=use_cache)

    def latest_source_page_context(
        self,
        document_id: str,
        workspace_id: str,
    ) -> dict[str, Any] | None:
        return database.latest_source_page_context(
            document_id,
            workspace_id,
        )

    def list_source_blocks(
        self,
        document_id: str,
    ) -> list[dict[str, Any]]:
        return database.list_source_blocks(document_id)


class ObjectStoragePipelineSourceReader:
    def read_text(self, object_uri: str) -> str:
        return read_text_object(object_uri)


class ObjectStoragePipelineLogReader:
    def read_text(self, path: str) -> str:
        try:
            return read_text_object(path)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return ""
            raise
