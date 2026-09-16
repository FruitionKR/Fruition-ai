import unittest

from app.modules.wiki_generation.application.evaluation_guards import (
    apply_generation_evaluation_guards,
    repair_normalized_from_evaluation,
)
from app.modules.wiki_generation.application.run_generation_loop import (
    EvaluationGuardRepairer,
)


class EvaluationGuardsTest(unittest.TestCase):
    def test_marks_every_actionable_generation_issue_as_retryable(self) -> None:
        evaluation = {
            "passed": True,
            "retry_recommended": False,
            "scores": {"overall": 0.95},
            "issues": [
                {
                    "type": "evidence_too_broad",
                    "severity": "low",
                    "target": ["ev_0001"],
                    "feedback": "근거 범위를 좁히세요.",
                }
            ],
            "retry_feedback": "",
        }
        normalized = {"concept_ledger": [], "observations": []}

        apply_generation_evaluation_guards(evaluation, normalized)

        self.assertFalse(evaluation["passed"])
        self.assertTrue(evaluation["retry_recommended"])
        self.assertEqual(evaluation["scores"]["overall"], 0.74)
        self.assertEqual(evaluation["issues"][0]["type"], "evidence_too_broad")
        self.assertIn("근거 범위를 좁히세요.", evaluation["retry_feedback"])

    def test_keeps_warning_only_evaluation_passed(self) -> None:
        evaluation = {
            "passed": True,
            "retry_recommended": False,
            "scores": {"overall": 0.9},
            "issues": [],
            "warnings": [{"type": "optional_improvement"}],
            "retry_feedback": "",
        }

        apply_generation_evaluation_guards(evaluation, {"concept_ledger": [], "observations": []})

        self.assertTrue(evaluation["passed"])
        self.assertFalse(evaluation["retry_recommended"])

    def test_rejects_unanchored_and_unknown_factual_refs(self) -> None:
        evaluation = {
            "passed": True,
            "retry_recommended": False,
            "scores": {"overall": 0.95},
            "issues": [],
            "retry_feedback": "",
        }
        normalized = {
            "concept_ledger": [{"slug": "artifact-flow", "title": "Artifact flow", "anchor_reference_ids": []}],
            "evidence_units": [{"evidence_id": "ev_0001", "claim": "근거", "anchor_reference_ids": ["B9999"]}],
        }

        apply_generation_evaluation_guards(evaluation, normalized, ["B0001"])

        self.assertFalse(evaluation["passed"])
        self.assertTrue(evaluation["retry_recommended"])
        self.assertEqual(
            {issue["type"] for issue in evaluation["issues"]},
            {"missing_ref", "invalid_ref"},
        )

    def test_rejects_any_factual_ref_when_source_allow_list_is_empty(self) -> None:
        evaluation = {
            "passed": True,
            "retry_recommended": False,
            "scores": {"overall": 0.95},
            "issues": [],
            "retry_feedback": "",
        }

        apply_generation_evaluation_guards(
            evaluation,
            {"evidence_units": [{"claim": "근거", "anchor_reference_ids": ["B9999"]}]},
            [],
        )

        self.assertEqual([issue["type"] for issue in evaluation["issues"]], ["invalid_ref"])

    def test_rejects_disallowed_direct_semantic_note_anchor(self) -> None:
        evaluation = {
            "passed": True,
            "retry_recommended": False,
            "scores": {"overall": 0.95},
            "issues": [],
            "retry_feedback": "",
        }

        apply_generation_evaluation_guards(
            evaluation,
            {
                "semantic_notes": [
                    {
                        "chunk_id": "chunk_0001",
                        "semantic_summary": "패킷 의미",
                        "anchor_reference_ids": ["B9999"],
                    }
                ]
            },
            ["B0001"],
        )

        self.assertFalse(evaluation["passed"])
        self.assertEqual([issue["type"] for issue in evaluation["issues"]], ["invalid_ref"])

    def test_accepts_allowed_direct_semantic_note_anchor(self) -> None:
        evaluation = {
            "passed": True,
            "retry_recommended": False,
            "scores": {"overall": 0.95},
            "issues": [],
            "retry_feedback": "",
        }

        apply_generation_evaluation_guards(
            evaluation,
            {
                "semantic_notes": [
                    {
                        "chunk_id": "chunk_0001",
                        "semantic_summary": "패킷 의미",
                        "anchor_reference_ids": ["B0001"],
                    }
                ]
            },
            ["B0001"],
        )

        self.assertTrue(evaluation["passed"])
        self.assertEqual(evaluation["issues"], [])

    def test_rejects_packet_without_anchored_meaning(self) -> None:
        evaluation = {
            "passed": True,
            "retry_recommended": False,
            "scores": {"overall": 0.95},
            "issues": [],
            "retry_feedback": "",
        }

        apply_generation_evaluation_guards(
            evaluation,
            {
                "semantic_notes": [
                    {"chunk_id": "chunk_0001", "semantic_summary": "패킷 의미"},
                    {
                        "chunk_id": "chunk_0002",
                        "key_points": [{"text": "근거", "anchor_reference_ids": ["B0002"]}],
                    },
                ]
            },
            ["B0001", "B0002"],
        )

        self.assertFalse(evaluation["passed"])
        self.assertEqual(evaluation["issues"][0]["type"], "semantic_coverage_gap")
        self.assertEqual(evaluation["issues"][0]["target"], ["chunk_0001"])

    def test_ignores_empty_and_navigation_only_packets(self) -> None:
        evaluation = {
            "passed": True,
            "retry_recommended": False,
            "scores": {"overall": 0.95},
            "issues": [],
            "retry_feedback": "",
        }

        apply_generation_evaluation_guards(
            evaluation,
            {
                "semantic_notes": [
                    {"chunk_id": "chunk_empty"},
                    {
                        "chunk_id": "chunk_navigation",
                        "needs_neighbor_context": True,
                        "context_problem": "다음 섹션과 연결 필요",
                    },
                ]
            },
            ["B0001"],
        )

        self.assertEqual(evaluation["issues"], [])

    def test_marks_failed_evaluation_retryable_even_without_issues(self) -> None:
        evaluation = {
            "passed": False,
            "retry_recommended": False,
            "scores": {},
            "issues": [],
            "retry_feedback": "",
        }

        apply_generation_evaluation_guards(evaluation, {})

        self.assertTrue(evaluation["retry_recommended"])

    def test_accepts_short_complete_korean_observation_summary(self) -> None:
        evaluation = {
            "passed": True,
            "retry_recommended": False,
            "scores": {"overall": 0.95},
            "issues": [],
            "retry_feedback": "",
        }

        apply_generation_evaluation_guards(
            evaluation,
            {
                "observations": [
                    {
                        "observation_id": "O001",
                        "title": "승인 상태",
                        "summary": "승인 대기",
                        "claims": [],
                        "anchor_reference_ids": ["B0001"],
                    }
                ]
            },
            ["B0001"],
        )

        self.assertEqual(evaluation["issues"], [])

    def test_repairs_broken_and_duplicate_observations(self) -> None:
        normalized = {
            "observations": [
                {
                    "observation_id": "O001",
                    "type": "qa_episode",
                    "title": "짧은 제목",
                    "query_text": "Back EMF 영향?",
                    "summary": "Back EMF는 제조 공차의 영향을 받는다.",
                    "claims": ["Back EMF는 공차 영향을 받는다."],
                    "anchor_reference_ids": ["B0001"],
                    "related_concept_hints": ["back-emf"],
                },
                {
                    "observation_id": "O002",
                    "type": "qa_episode",
                    "title": "더 긴 제목",
                    "query_text": "Back EMF 영향?",
                    "summary": "Back EMF는 제조 공차와 온도 조건의 영향을 받는다.",
                    "claims": ["Back EMF는 온도 조건 영향을 받는다."],
                    "anchor_reference_ids": ["B0002"],
                    "related_concept_hints": ["temperature"],
                },
                {
                    "observation_id": "O003",
                    "type": "qa_episode",
                    "title": "깨진 observation",
                    "query_text": "",
                    "summary": "-",
                    "claims": [],
                    "anchor_reference_ids": [],
                    "related_concept_hints": [],
                },
            ],
            "semantic_notes": [
                {
                    "observations": [
                        {
                            "type": "qa_episode",
                            "title": "짧은 제목",
                            "query_text": "Back EMF 영향?",
                            "summary": "Back EMF는 제조 공차의 영향을 받는다.",
                        },
                        {
                            "type": "qa_episode",
                            "title": "깨진 observation",
                            "query_text": "",
                            "summary": "-",
                        },
                    ]
                }
            ],
        }
        evaluation = {
            "issues": [
                {"type": "duplicate_observation", "target": ["O001", "O002"]},
                {"type": "broken_observation", "target": ["O003"]},
            ]
        }

        repaired, operations = repair_normalized_from_evaluation(normalized, evaluation)

        self.assertEqual([item["observation_id"] for item in repaired["observations"]], ["O001"])
        self.assertEqual(repaired["observations"][0]["anchor_reference_ids"], ["B0002", "B0001"])
        self.assertEqual(
            repaired["observations"][0]["claims"],
            ["Back EMF는 온도 조건 영향을 받는다.", "Back EMF는 공차 영향을 받는다."],
        )
        self.assertEqual(repaired["observations"][0]["related_concept_hints"], ["temperature", "back-emf"])
        self.assertEqual(len(repaired["semantic_notes"][0]["observations"]), 0)
        self.assertEqual(len(operations), 2)

    def test_repairer_removes_observation_from_raw_notes_and_normalized_result(self) -> None:
        notes = [
            {
                "chunk_id": "chunk_0001",
                "observations": [
                    {
                        "type": "source_claim",
                        "title": "깨진 관찰",
                        "summary": "짧음",
                        "claims": [],
                        "related_concept_hints": [],
                        "anchor_block_ids": ["B0001"],
                    }
                ],
            }
        ]
        normalized_observation = {
            "type": "source_claim",
            "title": "깨진 관찰",
            "query_text": None,
            "summary": "짧음",
            "claims": [],
            "related_concept_hints": [],
            "anchor_reference_ids": ["B0001"],
            "observation_id": "O001",
            "source_document_id": "doc-1",
        }
        normalized = {
            "observations": [normalized_observation],
            "semantic_notes": [
                {
                    "chunk_id": "chunk_0001",
                    "observations": [
                        {
                            key: value
                            for key, value in normalized_observation.items()
                            if key not in {"observation_id", "source_document_id"}
                        }
                    ],
                }
            ],
        }
        evaluation = {
            "issues": [{"type": "broken_observation", "target": ["O001"]}]
        }

        repaired_notes, operations = EvaluationGuardRepairer().repair(
            notes,
            normalized,
            evaluation,
        )

        self.assertEqual(repaired_notes[0]["observations"], [])
        self.assertTrue(operations)


if __name__ == "__main__":
    unittest.main()
