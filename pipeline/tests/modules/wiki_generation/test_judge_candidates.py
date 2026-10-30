from __future__ import annotations

import json
import unittest

from app.modules.wiki_generation.application.judge_candidates import (
    PROMOTION_BATCH_CHARS,
    judge_cluster_promotions,
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
                        "reason": "같은 개념의 다른 표기",
                    },
                    {"candidate_id": "unknown", "decision": "new_cluster"},
                ]
            }
        )

        existing = "# Active\n\n## cluster: back-emf\n\n### Evidence Claims\n- claim_001: 역기전력 [doc:B0001]"
        result = judge_meaning_cluster_candidates(
            completion=completion,
            existing_active_markdown=existing,
            candidates=[candidate()],
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["decision"], "same_cluster")
        self.assertNotIn("promotion_status", result[0])
        self.assertEqual(completion.payload["existing_active_clusters"], existing)
        self.assertIn("MeaningClusterJudge", completion.system_prompt)

    def test_judges_promotion_per_cluster_in_one_call_when_small(self) -> None:
        completion = FakeCompletion(
            {
                "decisions": [
                    {"cluster_id": "back-emf", "promotion_status": "candidate", "reason": "여러 문서에서 쌓임"},
                    {"cluster_id": "cogging", "promotion_status": "none"},
                    {"cluster_id": "unknown", "promotion_status": "candidate"},
                ]
            }
        )

        promoted = judge_cluster_promotions(
            completion=completion,
            clusters=[
                {"cluster_id": "back-emf", "section": "## cluster: back-emf"},
                {"cluster_id": "cogging", "section": "## cluster: cogging"},
            ],
        )

        self.assertEqual(promoted, {"back-emf": "여러 문서에서 쌓임"})
        self.assertEqual(
            [item["cluster_id"] for item in completion.payload["clusters"]],
            ["back-emf", "cogging"],
        )
        self.assertIn("independently", completion.system_prompt)

    def test_splits_promotion_calls_by_size_and_retries_missing_clusters_one_by_one(self) -> None:
        calls: list[list[str]] = []

        class SplittingCompletion:
            def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
                ids = [item["cluster_id"] for item in json.loads(user_prompt)["clusters"]]
                calls.append(ids)
                if len(ids) > 1:
                    raise ValueError("broken JSON")
                return {"decisions": [{"cluster_id": ids[0], "promotion_status": "candidate", "reason": ids[0]}]}

        big = "x" * (PROMOTION_BATCH_CHARS - 1)
        promoted = judge_cluster_promotions(
            completion=SplittingCompletion(),
            clusters=[
                {"cluster_id": "a", "section": "a"},
                {"cluster_id": "b", "section": "b"},
                {"cluster_id": "c", "section": big},
            ],
        )

        self.assertEqual(calls, [["a", "b"], ["a"], ["b"], ["c"]])
        self.assertEqual(promoted, {"a": "a", "b": "b", "c": "c"})

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

    def test_pair_judge_maps_same_and_related_and_sends_only_identity(self) -> None:
        calls = []

        def judge_pairs(incoming, pool):
            calls.append((incoming, pool))
            return {
                "cand_001": {"decision": "merge_into", "canonical_slug": "back-emf"},
                "cand_002": {"decision": "link_to", "canonical_slug": "back-emf"},
            }

        other = {**candidate(), "candidate_id": "cand_002", "term": "토크 리플", "slug": "torque-ripple"}
        third = {**candidate(), "candidate_id": "cand_003", "term": "코깅", "slug": "cogging"}
        completion = FakeCompletion({})

        decisions = judge_concept_update_candidates(
            completion=completion,
            concepts=[{
                "slug": "back-emf",
                "title": "역기전력",
                "aliases": ["Back EMF"],
                "definition": "전동기에서 유기되는 전압이다.",
                "why_page_worthy": "성능 평가에 필요하다.",
                "evidence": ["역기전력은 속도에 비례한다."],
            }],
            candidates=[candidate(), other, third],
            judge_pairs=judge_pairs,
        )

        self.assertEqual(
            [(item["decision"], item["concept_slug"], item["relation"]) for item in decisions],
            [("same_concept", "back-emf", "same_concept"), ("relation_candidate", "back-emf", "related_evidence"),
             ("not_same_concept", "", "")],
        )
        incoming, pool = calls[0]
        self.assertEqual(incoming[0], {
            "slug": "cand_001", "title": "Back EMF", "aliases": ["back-emf"], "definition": "역기전력은 속도에 비례한다.",
        })
        self.assertEqual(pool, [{
            "slug": "back-emf", "title": "역기전력", "aliases": ["Back EMF"], "definition": "전동기에서 유기되는 전압이다.",
        }])
        self.assertEqual(completion.payload, {})

    def test_falls_back_to_llm_without_evidence_when_pair_judge_is_unavailable(self) -> None:
        from app.core.jev_client import JevUnavailable

        def judge_pairs(_incoming, _pool):
            raise JevUnavailable("down")

        completion = FakeCompletion({"decisions": [{"candidate_id": "cand_001", "decision": "same_concept", "concept_slug": "back-emf"}]})

        decisions = judge_concept_update_candidates(
            completion=completion,
            concepts=[{"slug": "back-emf", "title": "역기전력", "definition": "정의", "evidence": ["근거"], "why_page_worthy": "이유"}],
            candidates=[candidate()],
            judge_pairs=judge_pairs,
        )

        self.assertEqual(decisions[0]["decision"], "same_concept")
        self.assertEqual(completion.payload["concepts"], [{"slug": "back-emf", "title": "역기전력", "aliases": [], "definition": "정의"}])
