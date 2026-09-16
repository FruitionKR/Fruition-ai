from __future__ import annotations

import unittest

from app.modules.wiki_generation.application.run_generation_loop import (
    EvaluationGuardRepairer,
    generation_evaluation_status,
    generation_retry_block_ids,
)
from app.modules.wiki_generation.infrastructure.wiki_generation_evaluator_graph import (
    LangGraphWikiGenerationEvaluator,
)


class FakeSemanticGeneration:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.target_block_ids: list[list[str] | None] = []
        self.patch_calls = 0
        self.patch_notes: list[list[dict[str, object]]] = []

    def generate(
        self,
        system_prompt: str,
        attempt: int,
        source_context: dict[str, object] | None,
        previous_notes: list[dict[str, object]] | None = None,
        target_block_ids: list[str] | None = None,
    ) -> list[dict[str, object]]:
        self.prompts.append(system_prompt)
        self.target_block_ids.append(target_block_ids)
        return [{"attempt": attempt, "source_context": source_context, "chunk_id": "chunk_0001"}]

    def patch(
        self,
        attempt: int,
        previous_notes: list[dict[str, object]],
        evaluation: dict[str, object],
        target_block_ids: list[str],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]] | None:
        self.patch_calls += 1
        self.patch_notes.append(previous_notes)
        return (
            [{**previous_notes[0], "attempt": attempt, "patched": True}],
            [{"op": "replace"}],
        )


class FakeNormalizer:
    def normalize_notes(self, notes: list[dict[str, object]]) -> dict[str, object]:
        normalized = {"attempt": notes[0]["attempt"]}
        if notes[0].get("repaired"):
            normalized["repaired"] = True
        return normalized


class TargetNormalizer:
    def normalize_notes(self, notes: list[dict[str, object]]) -> dict[str, object]:
        return {
            "attempt": notes[0]["attempt"],
            "concept_ledger": [
                {"slug": "target-concept", "anchor_reference_ids": ["B0002"]}
            ],
            "evidence_units": [],
        }


class FakeEvaluator:
    def __init__(self, evaluations: list[dict[str, object]]) -> None:
        self.evaluations = evaluations

    def evaluate(self, normalized: dict[str, object]) -> dict[str, object]:
        return self.evaluations.pop(0)


class FakeRepairer:
    def __init__(self, operations: list[str] | None = None) -> None:
        self.operations = operations or []

    def repair(
        self,
        notes: list[dict[str, object]],
        normalized: dict[str, object],
        evaluation: dict[str, object],
    ) -> tuple[list[dict[str, object]], list[str]]:
        if not self.operations:
            return notes, []
        return [{**notes[0], "repaired": True}], self.operations


class FakeEvents:
    def __init__(self) -> None:
        self.stages: list[str] = []

    def emit(self, stage: str, message: str, data: dict[str, object] | None = None) -> None:
        self.stages.append(stage)


class FakeEvaluationArtifacts:
    def __init__(self) -> None:
        self.items: list[tuple[int, str, dict[str, object]]] = []

    def write(self, attempt: int, kind: str, evaluation: dict[str, object]) -> None:
        self.items.append((attempt, kind, evaluation))


class LangGraphWikiGenerationEvaluatorTest(unittest.TestCase):
    def build_graph(
        self,
        evaluations: list[dict[str, object]],
        operations: list[str] | None = None,
    ) -> tuple[LangGraphWikiGenerationEvaluator, FakeSemanticGeneration, FakeEvaluationArtifacts]:
        semantic_generation = FakeSemanticGeneration()
        artifacts = FakeEvaluationArtifacts()
        graph = LangGraphWikiGenerationEvaluator(
            semantic_generation=semantic_generation,
            normalizer=FakeNormalizer(),
            evaluator=FakeEvaluator(evaluations),
            repairer=FakeRepairer(operations),
            events=FakeEvents(),
            evaluation_artifacts=artifacts,
        )
        return graph, semantic_generation, artifacts

    def test_returns_after_normalization_when_evaluation_is_disabled(self) -> None:
        graph, semantic_generation, artifacts = self.build_graph([])

        notes, normalized, evaluations = graph.run(
            semantic_system_prompt="기본 prompt",
            source_context={"source": "기존"},
            evaluation_enabled=False,
            max_attempts=2,
        )

        self.assertEqual(notes[0]["source_context"], {"source": "기존"})
        self.assertEqual(normalized, {"attempt": 1})
        self.assertEqual(evaluations, [])
        self.assertEqual(semantic_generation.prompts, ["기본 prompt"])
        self.assertEqual(artifacts.items, [])

    def test_retries_with_evaluator_feedback(self) -> None:
        graph, semantic_generation, artifacts = self.build_graph(
            [
                {"passed": False, "retry_recommended": True, "retry_feedback": "근거를 보강하세요.", "scores": {}, "issues": []},
                {"passed": True, "retry_recommended": False, "scores": {}, "issues": []},
            ]
        )

        notes, normalized, evaluations = graph.run(
            semantic_system_prompt="기본 prompt",
            source_context=None,
            evaluation_enabled=True,
            max_attempts=2,
        )

        self.assertEqual(notes[0]["attempt"], 2)
        self.assertEqual(normalized, {"attempt": 2})
        self.assertEqual(len(evaluations), 2)
        self.assertIn("근거를 보강하세요.", semantic_generation.prompts[1])
        self.assertEqual(
            [item[:2] for item in artifacts.items],
            [(1, "evaluation"), (1, "retry"), (2, "evaluation")],
        )

    def test_uses_repaired_normalized_result(self) -> None:
        graph, _, artifacts = self.build_graph(
            [
                {"passed": False, "retry_recommended": True, "scores": {}, "issues": []},
                {"passed": True, "retry_recommended": False, "scores": {}, "issues": []},
            ],
            operations=["replaced observation"],
        )

        _, normalized, evaluations = graph.run(
            semantic_system_prompt="기본 prompt",
            source_context=None,
            evaluation_enabled=True,
            max_attempts=2,
        )

        self.assertTrue(normalized["repaired"])
        self.assertEqual(len(evaluations), 2)
        self.assertEqual([item[:2] for item in artifacts.items], [(1, "evaluation"), (1, "repair")])

    def test_exposes_evaluator_nodes_as_langgraph_nodes(self) -> None:
        graph, _, _ = self.build_graph([])

        node_names = set(graph.graph.get_graph().nodes)

        self.assertTrue(
            {"semantic_generation", "normalize", "evaluate", "repair", "reevaluate", "prepare_retry", "targeted_patch"}.issubset(node_names)
        )

    def test_uses_targeted_patch_without_regenerating_chunk(self) -> None:
        semantic_generation = FakeSemanticGeneration()

        graph = LangGraphWikiGenerationEvaluator(
            semantic_generation=semantic_generation,
            normalizer=TargetNormalizer(),
            evaluator=FakeEvaluator(
                [
                    {
                        "passed": False,
                        "retry_recommended": True,
                        "retry_feedback": "개념을 수정하세요.",
                        "scores": {},
                        "issues": [{"target": ["target-concept"]}],
                    },
                    {"passed": True, "retry_recommended": False, "scores": {}, "issues": []},
                ]
            ),
            repairer=FakeRepairer(),
            events=FakeEvents(),
            evaluation_artifacts=FakeEvaluationArtifacts(),
        )

        notes, _, evaluations = graph.run(
            semantic_system_prompt="기본 prompt",
            source_context=None,
            evaluation_enabled=True,
            max_attempts=2,
        )

        self.assertTrue(notes[0]["patched"])
        self.assertEqual(semantic_generation.patch_calls, 1)
        self.assertEqual(len(semantic_generation.prompts), 1)
        self.assertEqual(evaluations[0]["retry_mode"], "targeted_patch")
        self.assertEqual(evaluations[0]["applied_patch_operations"], [{"op": "replace"}])

    def test_regenerates_targeted_chunk_when_patch_fails(self) -> None:
        class FailingPatchSemanticGeneration(FakeSemanticGeneration):
            def patch(
                self,
                attempt: int,
                previous_notes: list[dict[str, object]],
                evaluation: dict[str, object],
                target_block_ids: list[str],
            ) -> None:
                self.patch_calls += 1
                return None

        semantic_generation = FailingPatchSemanticGeneration()
        graph = LangGraphWikiGenerationEvaluator(
            semantic_generation=semantic_generation,
            normalizer=TargetNormalizer(),
            evaluator=FakeEvaluator(
                [
                    {
                        "passed": False,
                        "retry_recommended": True,
                        "retry_feedback": "개념을 수정하세요.",
                        "scores": {},
                        "issues": [{"target": ["target-concept"]}],
                    },
                    {"passed": True, "retry_recommended": False, "scores": {}, "issues": []},
                ]
            ),
            repairer=FakeRepairer(),
            events=FakeEvents(),
            evaluation_artifacts=FakeEvaluationArtifacts(),
        )

        _, _, evaluations = graph.run(
            semantic_system_prompt="기본 prompt",
            source_context=None,
            evaluation_enabled=True,
            max_attempts=2,
        )

        self.assertEqual(semantic_generation.patch_calls, 1)
        self.assertEqual(len(semantic_generation.prompts), 2)
        self.assertEqual(semantic_generation.target_block_ids[1], ["B0002"])
        self.assertEqual(evaluations[0]["retry_mode"], "targeted_chunk_regeneration")

    def test_deterministic_repair_remains_removed_during_following_patch(self) -> None:
        class ObservationSemanticGeneration(FakeSemanticGeneration):
            def generate(
                self,
                system_prompt: str,
                attempt: int,
                source_context: dict[str, object] | None,
                previous_notes: list[dict[str, object]] | None = None,
                target_block_ids: list[str] | None = None,
            ) -> list[dict[str, object]]:
                self.prompts.append(system_prompt)
                self.target_block_ids.append(target_block_ids)
                return [
                    {
                        "attempt": attempt,
                        "chunk_id": "chunk_0001",
                        "observations": [
                            {
                                "type": "source_claim",
                                "title": "깨진 관찰",
                                "summary": "짧음",
                                "anchor_block_ids": ["B0001"],
                            }
                        ],
                    }
                ]

        class RepairAwareNormalizer:
            def normalize_notes(self, notes: list[dict[str, object]]) -> dict[str, object]:
                raw_observations = list(notes[0].get("observations", []))
                normalized_observations = [
                    {
                        **observation,
                        "anchor_reference_ids": observation.get("anchor_block_ids", []),
                        "observation_id": f"O{index:03d}",
                    }
                    for index, observation in enumerate(raw_observations, start=1)
                ]
                return {
                    "attempt": notes[0]["attempt"],
                    "observations": normalized_observations,
                    "semantic_notes": [
                        {
                            "chunk_id": "chunk_0001",
                            "observations": [
                                {
                                    key: value
                                    for key, value in observation.items()
                                    if key != "observation_id"
                                }
                                for observation in normalized_observations
                            ],
                        }
                    ],
                    "concept_ledger": [
                        {"slug": "target-concept", "anchor_reference_ids": ["B0002"]}
                    ],
                    "evidence_units": [],
                }

        semantic_generation = ObservationSemanticGeneration()
        graph = LangGraphWikiGenerationEvaluator(
            semantic_generation=semantic_generation,
            normalizer=RepairAwareNormalizer(),
            evaluator=FakeEvaluator(
                [
                    {
                        "passed": False,
                        "retry_recommended": True,
                        "scores": {},
                        "issues": [
                            {"type": "broken_observation", "target": ["O001"]},
                            {"type": "evidence_too_broad", "target": ["target-concept"]},
                        ],
                    },
                    {
                        "passed": False,
                        "retry_recommended": True,
                        "retry_feedback": "개념을 수정하세요.",
                        "scores": {},
                        "issues": [{"type": "evidence_too_broad", "target": ["target-concept"]}],
                    },
                    {"passed": True, "retry_recommended": False, "scores": {}, "issues": []},
                ]
            ),
            repairer=EvaluationGuardRepairer(),
            events=FakeEvents(),
            evaluation_artifacts=FakeEvaluationArtifacts(),
        )

        graph.run(
            semantic_system_prompt="기본 prompt",
            source_context=None,
            evaluation_enabled=True,
            max_attempts=2,
        )

        self.assertEqual(semantic_generation.patch_notes[0][0]["observations"], [])

    def test_resolves_concept_and_evidence_targets_to_source_blocks(self) -> None:
        normalized = {
            "concept_ledger": [
                {
                    "slug": "target-concept",
                    "anchor_reference_ids": ["B0002"],
                    "evidence_claim_ids": ["ev_0001"],
                }
            ],
            "evidence_units": [
                {"evidence_id": "ev_0001", "anchor_reference_ids": ["B0003"]}
            ],
        }
        evaluation = {
            "issues": [{"target": ["target-concept"]}, {"target": ["ev_0001"]}]
        }

        self.assertEqual(
            generation_retry_block_ids(normalized, evaluation),
            ["B0002", "B0003"],
        )

    def test_resolves_packet_completeness_target_to_packet_anchors(self) -> None:
        self.assertEqual(
            generation_retry_block_ids(
                {
                    "semantic_notes": [
                        {
                            "chunk_id": "chunk_0002",
                            "semantic_summary": "근거가 누락된 패킷 의미",
                        }
                    ]
                },
                {"issues": [{"type": "semantic_coverage_gap", "target": ["chunk_0002"]}]},
                ["B0001", "B0002"],
                {"chunk_0002": ["B0002"]},
            ),
            ["B0002"],
        )

    def test_filters_untrusted_refs_from_targeted_retry(self) -> None:
        self.assertEqual(
            generation_retry_block_ids(
                {
                    "concept_ledger": [
                        {
                            "slug": "target-concept",
                            "anchor_reference_ids": ["B9999", "B0002"],
                        }
                    ]
                },
                {"issues": [{"target": ["target-concept"]}]},
                ["B0001", "B0002"],
            ),
            ["B0002"],
        )

    def test_falls_back_to_full_regeneration_for_unresolved_target(self) -> None:
        self.assertIsNone(
            generation_retry_block_ids(
                {"concept_ledger": [], "evidence_units": []},
                {"issues": [{"target": ["unknown-concept"]}]},
            )
        )

    def test_accepts_direct_source_block_target(self) -> None:
        self.assertEqual(
            generation_retry_block_ids({}, {"issues": [{"target": ["B0007"]}]}),
            ["B0007"],
        )

    def test_rejects_direct_source_block_target_missing_from_document(self) -> None:
        self.assertIsNone(
            generation_retry_block_ids(
                {},
                {"issues": [{"target": ["B9999"]}]},
                ["B0001", "B0002"],
            )
        )

    def test_records_full_regeneration_for_missing_source_block_target(self) -> None:
        semantic_generation = FakeSemanticGeneration()
        graph = LangGraphWikiGenerationEvaluator(
            semantic_generation=semantic_generation,
            normalizer=FakeNormalizer(),
            evaluator=FakeEvaluator(
                [
                    {
                        "passed": False,
                        "retry_recommended": True,
                        "retry_feedback": "해당 근거를 다시 확인하세요.",
                        "scores": {},
                        "issues": [{"target": ["B9999"]}],
                    },
                    {"passed": True, "retry_recommended": False, "scores": {}, "issues": []},
                ]
            ),
            repairer=FakeRepairer(),
            events=FakeEvents(),
            evaluation_artifacts=FakeEvaluationArtifacts(),
            source_block_ids=["B0001"],
        )

        _, _, evaluations = graph.run(
            semantic_system_prompt="기본 prompt",
            source_context=None,
            evaluation_enabled=True,
            max_attempts=2,
        )

        self.assertEqual(semantic_generation.patch_calls, 0)
        self.assertEqual(semantic_generation.target_block_ids, [None, None])
        self.assertEqual(evaluations[0]["retry_mode"], "full_regeneration")

    def test_records_unresolved_status_for_remaining_actionable_issue(self) -> None:
        self.assertEqual(generation_evaluation_status([]), "disabled")
        self.assertEqual(
            generation_evaluation_status([{"passed": True, "issues": []}]),
            "passed",
        )
        self.assertEqual(
            generation_evaluation_status([{"passed": False, "issues": [{"target": ["B0001"]}]}]),
            "unresolved",
        )
