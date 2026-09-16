from typing import Protocol

from app.modules.document_restoration.application.models import (
    PreparedRestoration,
    RestoreDocumentCommand,
    StageTiming,
)
from app.modules.document_restoration.domain.entities import RestorationStage


class DocumentRestorationStagesPort(Protocol):
    def prepare(self, command: RestoreDocumentCommand) -> PreparedRestoration:
        ...

    def needs_docling_baseline(self, prepared: PreparedRestoration) -> bool:
        ...

    def run_stage(
        self,
        stage: RestorationStage,
        command: RestoreDocumentCommand,
        prepared: PreparedRestoration,
    ) -> StageTiming:
        ...

    def write_timings(
        self,
        command: RestoreDocumentCommand,
        timings: list[StageTiming],
        total_elapsed_seconds: float,
    ) -> None:
        ...
