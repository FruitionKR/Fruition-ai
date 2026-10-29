import json
import unittest
from threading import Lock
from unittest.mock import patch

from app.core.jev_client import JevUnavailable
from app.modules.wiki_generation.infrastructure.jev_concept_resolver import (
    KEEP_NEW,
    JevConceptResolver,
    with_jev_concept_merge,
)


class RecordingResolver:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def resolve(self, incoming_concepts, existing_concepts, missing_related_hints=None):
        self.calls.append((incoming_concepts, existing_concepts, missing_related_hints))
        return {
            "resolutions": [{"incoming_slug": "llm", "decision": "merge_into", "canonical_slug": "x"}],
            "hint_resolutions": [{"hint_slug": "hint", "decision": "related_only", "canonical_slug": None}],
        }


class MappingJev:
    """incoming slug별로 정해 둔 선택을 돌려주고 받은 후보를 기록한다."""

    def __init__(self, choices: dict[str, str]) -> None:
        self._choices = choices
        self._lock = Lock()
        self.candidates: dict[str, list[str]] = {}

    def choose(self, state: str, questions: dict) -> dict:
        task = json.loads(state)["q0"]
        slug = task["incoming"]["slug"]
        with self._lock:
            self.candidates[slug] = [candidate["slug"] for candidate in task["candidates"]]
        choice = self._choices.get(slug, KEEP_NEW)
        assert choice in questions["q0"]["criteria"]
        return {"q0": {"choice": choice}}


class FailingJev:
    def choose(self, state: str, questions: dict) -> dict:
        raise JevUnavailable("Jev HTTP 402")


INCOMING = [
    {"slug": "memoization-new", "title": "메모이제이션", "definition": "함수 결과를 저장해 재계산을 피한다."},
    {"slug": "untitled", "title": "memoization", "definition": ""},
]
# DB 개념 인덱스는 definition, 파일 개념 인덱스는 summary를 쓴다.
EXISTING = [
    {"slug": "memoization", "title": "memoization", "aliases": ["메모이제이션"], "definition": "함수 결과를 저장해 재사용한다."},
    {"slug": "cache", "title": "cache", "aliases": [], "summary": "함수 결과를 포함해 데이터를 저장하는 포괄 개념이다."},
]


class JevConceptResolverTest(unittest.TestCase):
    def test_merges_chosen_candidate_and_skips_concepts_without_definition(self) -> None:
        jev = MappingJev({"memoization-new": "merge:memoization"})
        fallback = RecordingResolver()

        result = JevConceptResolver(jev, fallback).resolve(INCOMING, EXISTING, [])

        self.assertEqual(
            result["resolutions"],
            [{"incoming_slug": "memoization-new", "decision": "merge_into", "canonical_slug": "memoization", "alias_to_add": None}],
        )
        self.assertEqual(result["hint_resolutions"], [])
        self.assertNotIn("untitled", jev.candidates)
        self.assertEqual(set(jev.candidates["memoization-new"]), {"memoization", "cache"})
        self.assertLessEqual(len(jev.candidates["memoization-new"]), 3)
        self.assertEqual(fallback.calls, [])

    def test_keep_new_returns_no_resolution(self) -> None:
        result = JevConceptResolver(MappingJev({}), RecordingResolver()).resolve(INCOMING, EXISTING)

        self.assertEqual(result["resolutions"], [])

    def test_hints_use_existing_resolver_hint_result_only(self) -> None:
        fallback = RecordingResolver()
        hints = [{"slug": "hint"}]

        result = JevConceptResolver(MappingJev({}), fallback).resolve(INCOMING, EXISTING, hints)

        self.assertEqual(result["resolutions"], [])
        self.assertEqual(result["hint_resolutions"][0]["hint_slug"], "hint")
        self.assertEqual(fallback.calls, [(INCOMING, EXISTING, hints)])

    def test_unavailable_jev_uses_existing_resolver_result(self) -> None:
        fallback = RecordingResolver()

        result = JevConceptResolver(FailingJev(), fallback).resolve(INCOMING, EXISTING, [])

        self.assertEqual(result["resolutions"][0]["incoming_slug"], "llm")
        self.assertEqual(len(fallback.calls), 1)

    def test_drops_merge_into_incoming_concept_that_merges_elsewhere(self) -> None:
        incoming = [
            {"slug": "a", "title": "평균", "definition": "합을 개수로 나눈 값이다."},
            {"slug": "b", "title": "산술평균", "definition": "합을 개수로 나눈 값이다."},
        ]
        jev = MappingJev({"a": "merge:b", "b": "merge:mean"})
        existing = [{"slug": "mean", "title": "mean", "aliases": [], "summary": "합을 개수로 나눈 값이다."}]

        result = JevConceptResolver(jev, RecordingResolver()).resolve(incoming, existing)

        self.assertEqual([item["incoming_slug"] for item in result["resolutions"]], ["b"])


    def test_link_choice_keeps_concept_new_and_links_it(self) -> None:
        jev = MappingJev({"memoization-new": "link:cache"})

        result = JevConceptResolver(jev, RecordingResolver()).resolve(INCOMING, EXISTING, [])

        self.assertEqual(
            result["resolutions"],
            [{"incoming_slug": "memoization-new", "decision": "link_to", "canonical_slug": "cache", "alias_to_add": None}],
        )

    def test_link_to_a_merged_incoming_concept_is_left_to_the_slug_map(self) -> None:
        # 병합을 따라가는 처리는 실제로 적용된 병합(concept_slug_map)을 보는 LinkBuilder가 맡는다.
        incoming = [
            {"slug": "a", "title": "평균", "definition": "합을 개수로 나눈 값이다."},
            {"slug": "b", "title": "가중 평균", "definition": "가중치를 곱한 합을 가중치 합으로 나눈 값이다."},
        ]
        existing = [{"slug": "mean", "title": "mean", "aliases": [], "summary": "합을 개수로 나눈 값이다."}]
        jev = MappingJev({"a": "merge:mean", "b": "link:a"})

        result = JevConceptResolver(jev, RecordingResolver()).resolve(incoming, existing)

        self.assertIn({"incoming_slug": "b", "decision": "link_to", "canonical_slug": "a", "alias_to_add": None},
                      result["resolutions"])


class WithJevConceptMergeTest(unittest.TestCase):
    def test_keeps_existing_resolver_when_disabled(self) -> None:
        resolver = RecordingResolver()
        with patch.dict("os.environ", {"JEV_CONCEPT_MERGE_ENABLED": "false", "TYPESAFE_API_KEY": "key"}):
            self.assertIs(with_jev_concept_merge(resolver), resolver)

    def test_wraps_resolver_when_enabled_with_key(self) -> None:
        with patch.dict("os.environ", {"JEV_CONCEPT_MERGE_ENABLED": "true", "TYPESAFE_API_KEY": "key"}):
            self.assertIsInstance(with_jev_concept_merge(RecordingResolver()), JevConceptResolver)


if __name__ == "__main__":
    unittest.main()
