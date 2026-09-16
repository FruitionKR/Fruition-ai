from __future__ import annotations

from typing import Any, Protocol, Sequence

from app.modules.wiki_generation.application.models import GenerationEvaluation
from app.modules.wiki_generation.domain.entities import SemanticPacket, SourceBlock


JsonDict = dict[str, Any]


class SemanticExtractor(Protocol):
    def extract(self, packet: SemanticPacket) -> JsonDict:
        ...


class ConceptPageGenerator(Protocol):
    def generate(self, concept: JsonDict, evidence_units: list[JsonDict], source_blocks: Sequence[SourceBlock]) -> JsonDict:
        ...


class ConceptResolver(Protocol):
    def resolve(
        self,
        incoming_concepts: list[JsonDict],
        existing_concepts: list[JsonDict],
        missing_related_hints: list[JsonDict] | None = None,
    ) -> JsonDict:
        ...


class SectionPolisher(Protocol):
    def polish(self, payload: JsonDict, source_blocks: Sequence[SourceBlock]) -> JsonDict:
        ...


class JsonCompletionPort(Protocol):
    def complete_json(self, system_prompt: str, user_prompt: str) -> JsonDict:
        ...


class SemanticGenerationPort(Protocol):
    def generate(
        self,
        system_prompt: str,
        attempt: int,
        source_context: JsonDict | None,
        previous_notes: list[JsonDict] | None = None,
        target_block_ids: list[str] | None = None,
    ) -> list[JsonDict]:
        ...

    def patch(
        self,
        attempt: int,
        previous_notes: list[JsonDict],
        evaluation: GenerationEvaluation,
        target_block_ids: list[str],
    ) -> tuple[list[JsonDict], list[JsonDict]] | None:
        ...


class SemanticNormalizerPort(Protocol):
    def normalize_notes(self, notes: list[JsonDict]) -> JsonDict:
        ...


class GenerationEvaluatorPort(Protocol):
    def evaluate(self, normalized: JsonDict) -> GenerationEvaluation:
        ...


class GenerationRepairPort(Protocol):
    def repair(
        self,
        notes: list[JsonDict],
        normalized: JsonDict,
        evaluation: GenerationEvaluation,
    ) -> tuple[list[JsonDict], list[str]]:
        ...


class PipelineEventPort(Protocol):
    def emit(self, stage: str, message: str, data: JsonDict | None = None) -> None:
        ...


class EvaluationArtifactPort(Protocol):
    def write(self, attempt: int, kind: str, evaluation: GenerationEvaluation) -> None:
        ...
