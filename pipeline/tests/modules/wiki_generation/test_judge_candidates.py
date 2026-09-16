from __future__ import annotations

import json
import unittest

from app.modules.wiki_generation.application.judge_candidates import (
    judge_concept_update_candidates,
    judge_meaning_cluster_candidates,
    normalize_concept_update_decisions,
)


class FakeCompletion:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.system_prompt = ""
        self.payload: dict[str, object] = {}

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
        self.system_prompt = system_prompt
        self.payload = json.loads(user_prompt)
        return self.response


def candidate() -> dict[str, object]:
    return {
        "candidate_id": "cand_001",
        "term": "Back EMF",
        "slug": "back-emf",
        "claim": "역기전력은 속도에 비례한다.",
        "refs": ["B0001"],
        "candidate_type": "section_candidate",
    }


class JudgeCandidatesTest(unittest.TestCase):
    def test_normalizes_meaning_cluster_decision(self) -> None:
        completion = FakeCompletion(
            {
                "decisions": [
                    {
                        "candidate_id": "cand_001",
                        "decision": "same_cluster",
                        "target_cluster_id": "back-emf",
                        "representative": "Back EMF",
                        "promotion_status": "candidate",
                        "reason": "기존 근거가 충분함",
                    },
                    {"candidate_id": "unknown", "decision": "new_cluster"},
                ]
            }
        )

        result = judge_meaning_cluster_candidates(
            completion=completion,
            existing_active_markdown="# Active",
            candidates=[candidate()],
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["decision"], "same_cluster")
        self.assertEqual(result[0]["promotion_status"], "candidate")
        self.assertEqual(completion.payload["existing_active_clusters"], "# Active")
        self.assertIn("MeaningClusterJudge", completion.system_prompt)

    def test_concept_update_prompt_excludes_related_subtopics_from_same_concept(self) -> None:
        completion = FakeCompletion({"decisions": []})
        related_subtopic = {
            **candidate(),
            "term": "과습 관리 기록",
            "slug": "overwatering-management-record",
            "claim": "관수 제어 결과를 과습 관리 기록에 남긴다.",
        }

        judge_concept_update_candidates(
            completion=completion,
            concepts=[{"slug": "irrigation-control", "title": "관수 제어"}],
            candidates=[related_subtopic],
        )

        self.assertIn("A procedure, record, metric, property, evidence, or subtopic", completion.system_prompt)
        self.assertIn("is not same_concept", completion.system_prompt)

    def test_rejects_relation_to_unknown_concept(self) -> None:
        completion = FakeCompletion(
            {
                "concept_update_decisions": [
                    {
                        "candidate_id": "cand_001",
                        "decision": "relation_candidate",
                        "concept_slug": "missing-concept",
                        "relation": "part_of",
                    }
                ]
            }
        )

        result = normalize_concept_update_decisions(
            completion.response,
            concepts=[{"slug": "motor", "title": "Motor"}],
            candidates=[candidate()],
        )

        self.assertEqual(
            result[0],
            {
                "candidate_id": "cand_001",
                "decision": "not_same_concept",
                "concept_slug": "",
                "relation": "",
                "reason": None,
            },
        )

    def test_remaps_update_target_to_resolved_canonical_slug(self) -> None:
        result = normalize_concept_update_decisions(
            {
                "concept_update_decisions": [
                    {
                        "candidate_id": "cand_001",
                        "decision": "same_concept",
                        "concept_slug": "counter-emf",
                    }
                ]
            },
            concepts=[{"slug": "back-emf", "title": "Back EMF"}],
            candidates=[candidate()],
            concept_slug_map={"counter-emf": "back-emf"},
        )

        self.assertEqual(result[0]["decision"], "same_concept")
        self.assertEqual(result[0]["concept_slug"], "back-emf")

    def test_skips_concept_update_completion_when_inputs_are_empty(self) -> None:
        completion = FakeCompletion({"decisions": []})

        result = judge_concept_update_candidates(
            completion=completion,
            concepts=[],
            candidates=[candidate()],
        )

        self.assertEqual(result, [])
        self.assertEqual(completion.system_prompt, "")

    def test_ignores_malformed_and_unknown_candidate_decisions(self) -> None:
        completion = FakeCompletion(
            {
                "decisions": [
                    "invalid",
                    {"candidate_id": "unknown", "decision": "new_cluster"},
                ]
            }
        )

        result = judge_meaning_cluster_candidates(
            completion=completion,
            existing_active_markdown="# Active",
            candidates=[candidate()],
        )

        self.assertEqual(result, [])
