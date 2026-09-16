from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace

from app.modules.wiki_generation.infrastructure.generation_loop_adapters import (
    EvaluationArtifactAdapter,
    SemanticGenerationAdapter,
)


class FakeCompletion:
    def __init__(self) -> None:
        self.user_prompts: list[str] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
        self.user_prompts.append(user_prompt)
        chunk_id = "chunk_0001" if "B0001" in user_prompt else "chunk_0002"
        return {"chunk_id": chunk_id, "key_points": []}


class FakeEvents:
    def __init__(self) -> None:
        self.stages: list[str] = []

    def emit(self, stage: str, message: str, data: dict[str, object] | None = None) -> None:
        self.stages.append(stage)


class FakePatchCompletion:
    def __init__(self) -> None:
        self.user_prompt = ""

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
        self.user_prompt = user_prompt
        return {
            "operations": [
                {
                    "op": "replace",
                    "chunk_id": "chunk_0001",
                    "collection": "evidence_claims",
                    "index": 0,
                    "items": [
                        {"claim": "수정된 주장", "anchor_block_ids": ["B0002"]}
                    ],
                }
            ]
        }


class BlockingCompletion:
    def __init__(self) -> None:
        self.barrier = Barrier(2)

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
        self.barrier.wait(timeout=1)
        chunk_id = "chunk_0001" if "B0001" in user_prompt else "chunk_0002"
        return {"chunk_id": chunk_id, "key_points": []}


class SemanticGenerationAdapterTest(unittest.TestCase):
    def test_rejects_non_positive_worker_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be at least 1"):
            SemanticGenerationAdapter(
                FakeCompletion(),
                [],
                None,
                FakeEvents(),
                max_workers=0,
            )

    def test_extracts_packets_concurrently_and_preserves_order(self) -> None:
        packets = [
            SimpleNamespace(chunk_id="chunk_0001", document_id="doc-1", block_ids=["B0001"], text="[B0001] 첫째"),
            SimpleNamespace(chunk_id="chunk_0002", document_id="doc-1", block_ids=["B0002"], text="[B0002] 둘째"),
        ]
        adapter = SemanticGenerationAdapter(
            BlockingCompletion(),
            packets,
            None,
            FakeEvents(),
            max_workers=2,
        )

        notes = adapter.generate("prompt", 1, None)

        self.assertEqual(
            [note["chunk_id"] for note in notes],
            ["chunk_0001", "chunk_0002"],
        )

    def test_regenerates_only_packets_containing_target_blocks(self) -> None:
        completion = FakeCompletion()
        events = FakeEvents()
        packets = [
            SimpleNamespace(chunk_id="chunk_0001", document_id="doc-1", block_ids=["B0001"], text="[B0001] 첫째"),
            SimpleNamespace(chunk_id="chunk_0002", document_id="doc-1", block_ids=["B0002"], text="[B0002] 둘째"),
        ]
        previous_notes = [
            {"chunk_id": "chunk_0001", "semantic_summary": "기존 첫째"},
            {"chunk_id": "chunk_0002", "semantic_summary": "기존 둘째"},
        ]
        adapter = SemanticGenerationAdapter(completion, packets, None, events)

        notes = adapter.generate(
            "prompt",
            2,
            None,
            previous_notes=previous_notes,
            target_block_ids=["B0002"],
        )

        self.assertIs(notes[0], previous_notes[0])
        self.assertEqual(notes[1]["chunk_id"], "chunk_0002")
        self.assertEqual(len(completion.user_prompts), 1)
        self.assertEqual(events.stages, ["3. 의미 추출 재사용", "3. 의미 추출"])

    def test_regenerates_all_packets_when_target_matches_no_packet(self) -> None:
        completion = FakeCompletion()
        packets = [
            SimpleNamespace(chunk_id="chunk_0001", document_id="doc-1", block_ids=["B0001"], text="[B0001] 첫째"),
            SimpleNamespace(chunk_id="chunk_0002", document_id="doc-1", block_ids=["B0002"], text="[B0002] 둘째"),
        ]
        adapter = SemanticGenerationAdapter(completion, packets, None, FakeEvents())

        adapter.generate(
            "prompt",
            2,
            None,
            previous_notes=[{"chunk_id": "chunk_0001"}, {"chunk_id": "chunk_0002"}],
            target_block_ids=["B9999"],
        )

        self.assertEqual(len(completion.user_prompts), 2)

    def test_patch_sends_only_target_and_neighbor_blocks(self) -> None:
        completion = FakePatchCompletion()
        events = FakeEvents()
        blocks = [
            SimpleNamespace(block_id=f"B000{index}", text=f"본문 {index}")
            for index in range(1, 5)
        ]
        adapter = SemanticGenerationAdapter(
            completion,
            [],
            None,
            events,
            blocks,
            "patch prompt",
        )

        patch_result = adapter.patch(
            2,
            [
                {
                    "chunk_id": "chunk_0001",
                    "evidence_claims": [
                        {"claim": "기존 주장", "anchor_block_ids": ["B0002"]}
                    ],
                }
            ],
            {"issues": [{"target": ["ev_0001"]}], "retry_feedback": "원자화"},
            ["B0002"],
        )

        self.assertIsNotNone(patch_result)
        notes, operations = patch_result
        self.assertEqual(notes[0]["evidence_claims"][0]["claim"], "수정된 주장")
        self.assertEqual(operations[0]["op"], "replace")
        self.assertIn('"block_id": "B0001"', completion.user_prompt)
        self.assertIn('"block_id": "B0003"', completion.user_prompt)
        self.assertNotIn('"block_id": "B0004"', completion.user_prompt)
        self.assertEqual(events.stages, ["3-수정. 의미 구조 patch"])


class EvaluationArtifactAdapterTest(unittest.TestCase):
    def test_writes_retry_decision_to_separate_artifact(self) -> None:
        with TemporaryDirectory() as directory:
            out = Path(directory)
            adapter = EvaluationArtifactAdapter(out, True)

            adapter.write(1, "evaluation", {"passed": False})
            adapter.write(1, "retry", {"retry_mode": "targeted_patch"})

            artifact_dir = out / "raw_llm_outputs" / "wiki_evaluation"
            self.assertTrue((artifact_dir / "attempt_01.json").exists())
            self.assertTrue((artifact_dir / "attempt_01.retry.json").exists())


if __name__ == "__main__":
    unittest.main()
