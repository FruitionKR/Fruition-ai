from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from time import perf_counter
from typing import Any

from app.core.llm_env import int_env
from app.modules.wiki_generation.application.evaluate_generation import (
    evaluate_generation,
)
from app.modules.wiki_generation.application.models import GenerationEvaluation
from app.modules.wiki_generation.application.ports import (
    JsonCompletionPort,
    PipelineEventPort,
)
from app.modules.wiki_generation.application.semantic_patch import (
    apply_semantic_patch,
    build_semantic_patch_targets,
)
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    ApiSemanticExtractor,
)
from app.modules.wiki_ingestion.infrastructure.file_io import ensure_dir, write_json


class SemanticGenerationAdapter:
    def __init__(
        self,
        completion: JsonCompletionPort,
        packets: list[Any],
        raw_dir: Path | None,
        events: PipelineEventPort,
        blocks: list[Any] | None = None,
        patch_system_prompt: str = "",
        max_workers: int | None = None,
    ) -> None:
        self.completion = completion
        self.packets = packets
        self.raw_dir = raw_dir
        self.events = events
        self.blocks = blocks or []
        self.patch_system_prompt = patch_system_prompt
        self.max_workers = (
            max_workers
            if max_workers is not None
            else int_env("WIKI_SEMANTIC_MAX_WORKERS", 4)
        )
        if self.max_workers < 1:
            raise ValueError("WIKI_SEMANTIC_MAX_WORKERS must be at least 1")

    def generate(
        self,
        system_prompt: str,
        attempt: int,
        source_context: dict[str, Any] | None,
        previous_notes: list[dict[str, Any]] | None = None,
        target_block_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        extractor = ApiSemanticExtractor(
            self.completion,
            system_prompt,
            source_context=source_context,
        )
        previous_by_chunk = {
            str(note.get("chunk_id")): note
            for note in previous_notes or []
            if note.get("chunk_id")
        }
        target_blocks = set(target_block_ids or [])
        targeted_retry = bool(
            previous_notes is not None
            and target_block_ids is not None
            and any(target_blocks.intersection(packet.block_ids) for packet in self.packets)
        )
        work: list[tuple[Any, dict[str, Any] | None]] = []
        packets_to_extract = []
        for packet in self.packets:
            previous_note = previous_by_chunk.get(packet.chunk_id)
            should_regenerate = not targeted_retry or bool(target_blocks.intersection(packet.block_ids))
            if not should_regenerate and previous_note is not None:
                work.append((packet, previous_note))
                continue
            work.append((packet, None))
            packets_to_extract.append(packet)

        def extract(packet: Any) -> tuple[dict[str, Any], float]:
            started_at = perf_counter()
            return extractor.extract(packet), perf_counter() - started_at

        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(packets_to_extract) or 1)
        ) as executor:
            futures = [executor.submit(copy_context().run, extract, packet) for packet in packets_to_extract]
            extracted = iter(future.result() for future in futures)

        notes = []
        for packet, previous_note in work:
            if previous_note is not None:
                notes.append(previous_note)
                self.events.emit(
                    "3. 의미 추출 재사용",
                    "평가 target과 무관한 패킷의 기존 의미 노트를 유지했습니다.",
                    {"시도": attempt, "패킷": packet.chunk_id},
                )
                continue
            note, elapsed_seconds = next(extracted)
            notes.append(note)
            if self.raw_dir is not None:
                suffix = "" if attempt == 1 else f".attempt{attempt}"
                write_json(self.raw_dir / f"{packet.chunk_id}{suffix}.json", note)
            self.events.emit(
                "3. 의미 추출",
                "패킷에서 의미 노트를 추출했고, 노트 객체를 메모리에 추가했습니다.",
                {
                    "시도": attempt,
                    "패킷": packet.chunk_id,
                    "핵심 포인트 수": len(note.get("key_points", [])),
                    "core concept 수": len(note.get("core_concepts") or note.get("concept_candidates", [])),
                    "section/mention/category 수": f"{len(note.get('section_candidates', []))}/{len(note.get('mentions', []))}/{len(note.get('categories', []))}",
                    "근거 주장 수": len(note.get("evidence_claims", [])),
                    "소요 시간(초)": f"{elapsed_seconds:.2f}",
                },
            )
        return notes

    def patch(
        self,
        attempt: int,
        previous_notes: list[dict[str, Any]],
        evaluation: GenerationEvaluation,
        target_block_ids: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        if not self.patch_system_prompt:
            return None
        editable_targets = build_semantic_patch_targets(
            previous_notes,
            evaluation,
            target_block_ids,
        )
        if not editable_targets:
            return None
        context_blocks = self._patch_context_blocks(target_block_ids)
        if not context_blocks:
            return None
        payload = {
            "evaluator_issues": evaluation.get("issues", []),
            "retry_feedback": evaluation.get("retry_feedback", ""),
            "editable_targets": editable_targets,
            "source_blocks": [
                {"block_id": block.block_id, "text": block.text}
                for block in context_blocks
            ],
        }
        try:
            patch = self.completion.complete_json(
                self.patch_system_prompt,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except Exception as exc:
            self.events.emit(
                "3-수정. 의미 구조 patch 호출 실패",
                "Targeted patch LLM 호출에 실패해 해당 chunk 재생성으로 전환합니다.",
                {"시도": attempt, "오류": str(exc)},
            )
            return None
        if self.raw_dir is not None:
            write_json(self.raw_dir / f"semantic_patch.attempt{attempt}.json", patch)
        patched_notes = apply_semantic_patch(
            previous_notes,
            patch,
            editable_targets,
            [block.block_id for block in context_blocks],
        )
        if patched_notes is None:
            self.events.emit(
                "3-수정. 의미 구조 patch 실패",
                "Patch 결과가 허용된 target 또는 source anchor 계약을 벗어나 해당 chunk 재생성으로 전환합니다.",
                {"시도": attempt, "target blocks": target_block_ids},
            )
            return None
        self.events.emit(
            "3-수정. 의미 구조 patch",
            "Evaluator target과 연결된 의미 항목만 수정했습니다.",
            {
                "target blocks": target_block_ids,
                "시도": attempt,
                "수정 가능 항목 수": len(editable_targets),
                "operation 수": len(patch.get("operations", [])),
            },
        )
        return patched_notes, list(patch.get("operations", []))

    def _patch_context_blocks(self, target_block_ids: list[str]) -> list[Any]:
        target_ids = set(target_block_ids)
        target_indexes = [
            index
            for index, block in enumerate(self.blocks)
            if block.block_id in target_ids
        ]
        context_indexes = {
            neighbor
            for index in target_indexes
            for neighbor in (index - 1, index, index + 1)
            if 0 <= neighbor < len(self.blocks)
        }
        return [self.blocks[index] for index in sorted(context_indexes)]


class GenerationEvaluatorAdapter:
    def __init__(
        self,
        completion: JsonCompletionPort,
        evaluator_prompt: str,
        document: Any,
        blocks: list[Any],
    ) -> None:
        self.completion = completion
        self.evaluator_prompt = evaluator_prompt
        self.document = document
        self.blocks = blocks

    def evaluate(self, normalized: dict[str, Any]) -> GenerationEvaluation:
        return evaluate_generation(
            completion=self.completion,
            evaluator_prompt=self.evaluator_prompt,
            document=self.document,
            blocks=self.blocks,
            normalized=normalized,
        )


class EvaluationArtifactAdapter:
    def __init__(self, out: Path, enabled: bool) -> None:
        self.out = out
        self.enabled = enabled

    def write(
        self,
        attempt: int,
        kind: str,
        evaluation: GenerationEvaluation,
    ) -> None:
        if not self.enabled:
            return
        suffix = {"repair": ".repair", "retry": ".retry"}.get(kind, "")
        path = ensure_dir(self.out / "raw_llm_outputs" / "wiki_evaluation") / f"attempt_{attempt:02d}{suffix}.json"
        write_json(path, evaluation)
