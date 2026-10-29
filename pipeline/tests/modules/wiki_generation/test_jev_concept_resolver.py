import json
import math
import unittest
from threading import Lock
from unittest.mock import patch

from app.core.jev_batches import MAX_BATCH_TOKENS, request_tokens
from app.core.jev_client import JevUnavailable
from app.modules.wiki_generation.infrastructure import jev_concept_resolver
from app.modules.wiki_generation.infrastructure.jev_concept_resolver import (
    CANDIDATE_LIMIT,
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


class PairJev:
    """(incoming, 후보)별로 정해 둔 선택·확률을 돌려주고 받은 요청을 기록한다. 정하지 않은 쌍은 unrelated다."""

    def __init__(self, answers: dict[tuple[str, str], tuple[str, dict[str, float]]] | None = None) -> None:
        self._answers = answers or {}
        self._lock = Lock()
        self.requests: list[dict] = []

    def choose(self, state: str, questions: dict) -> dict:
        task = json.loads(state)
        with self._lock:
            self.requests.append({"state": state, "questions": questions})
        incoming = task["incoming"]["slug"]
        assert [candidate["id"] for candidate in task["candidates"]] == list(questions)
        result = {}
        for key, question in questions.items():
            choice, probabilities = self._answers.get((incoming, key), ("unrelated", {"unrelated": 0.9}))
            assert choice in question["criteria"]
            result[key] = {"choice": choice, "probabilities": probabilities}
        return result

    def sent(self, incoming: str) -> list[str]:
        return [
            key
            for request in self.requests
            if json.loads(request["state"])["incoming"]["slug"] == incoming
            for key in request["questions"]
        ]


class FailingJev:
    def choose(self, state: str, questions: dict) -> dict:
        raise JevUnavailable("Jev HTTP 402")


class AngleEmbedder:
    """텍스트 첫 단어에 정해 둔 각도의 2차원 단위 벡터를 돌려준다. 정하지 않은 단어는 직교 벡터다."""

    model_name = "fake-embedding"

    def __init__(self, angles: dict[str, float] | None = None) -> None:
        self._angles = angles or {}
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        angles = [self._angles.get(text.split()[0], math.pi / 2) for text in texts]
        return [[math.cos(angle), math.sin(angle)] for angle in angles]


class FailingEmbedder:
    model_name = "failing-embedding"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding server down")


INCOMING = [
    {"slug": "memoization-new", "title": "메모이제이션 기법", "definition": "함수 결과를 저장해 재계산을 피한다."},
    {"slug": "untitled", "title": "memoization", "definition": ""},
]
# DB 개념 인덱스는 definition, 파일 개념 인덱스는 summary를 쓴다.
EXISTING = [
    {"slug": "memoization", "title": "memoization", "aliases": ["메모"], "definition": "함수 결과를 저장해 재사용한다."},
    {"slug": "cache", "title": "cache", "aliases": [], "summary": "데이터를 저장하는 포괄 개념이다."},
    {"slug": "dynamic-programming", "title": "dynamic programming", "aliases": [], "definition": "부분 문제를 저장한다."},
]


def _resolver(jev, embedder=None, fallback=None) -> JevConceptResolver:
    return JevConceptResolver(jev, fallback or RecordingResolver(), embedder or AngleEmbedder())


class JevConceptResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        jev_concept_resolver._embedding_cache.clear()

    def test_links_all_related_candidates_ordered_by_probability(self) -> None:
        jev = PairJev({
            ("memoization-new", "cache"): ("related", {"related": 0.6, "unrelated": 0.4}),
            ("memoization-new", "dynamic-programming"): ("related", {"related": 0.8, "unrelated": 0.2}),
        })

        result = _resolver(jev).resolve(INCOMING, EXISTING, [])

        self.assertEqual(
            result["resolutions"],
            [{
                "incoming_slug": "memoization-new",
                "decision": "link_to",
                "canonical_slug": "dynamic-programming",
                "link_targets": ["dynamic-programming", "cache"],
                "link_confidences": {"dynamic-programming": 0.8, "cache": 0.6},
                "alias_to_add": None,
            }],
        )
        # 정의가 없는 개념은 Jev에 보내지 않고 후보에도 넣지 않는다.
        self.assertEqual(jev.sent("untitled"), [])
        self.assertEqual(set(jev.sent("memoization-new")), {"memoization", "cache", "dynamic-programming"})

    def test_merges_into_candidate_with_highest_same_probability(self) -> None:
        jev = PairJev({
            ("memoization-new", "memoization"): ("same", {"same": 0.7}),
            ("memoization-new", "cache"): ("same", {"same": 0.9}),
            ("memoization-new", "dynamic-programming"): ("related", {"related": 0.99}),
        })

        result = _resolver(jev).resolve(INCOMING, EXISTING, [])

        self.assertEqual(
            result["resolutions"],
            [{"incoming_slug": "memoization-new", "decision": "merge_into", "canonical_slug": "cache", "alias_to_add": None}],
        )

    def test_all_unrelated_returns_no_resolution(self) -> None:
        result = _resolver(PairJev()).resolve(INCOMING, EXISTING)

        self.assertEqual(result["resolutions"], [])

    def test_sends_only_top_candidates_by_hybrid_score(self) -> None:
        existing = [
            {"slug": f"c{index:03d}", "title": f"c{index:03d}", "aliases": [], "definition": f"정의{index}"}
            for index in range(CANDIDATE_LIMIT + 20)
        ]
        incoming = [{"slug": "query", "title": "query", "definition": "새 개념"}]
        # 번호가 클수록 incoming과의 각도가 커져 임베딩 점수가 낮다.
        angles = {"query": 0.0, **{concept["slug"]: index / 100 for index, concept in enumerate(existing)}}
        jev = PairJev()

        _resolver(jev, AngleEmbedder(angles)).resolve(incoming, existing)

        self.assertEqual(sorted(jev.sent("query")), [concept["slug"] for concept in existing[:CANDIDATE_LIMIT]])

    def test_splits_questions_into_requests_by_token_budget(self) -> None:
        existing = [
            {"slug": f"c{index:03d}", "title": f"c{index:03d}", "aliases": [], "definition": "긴 정의 " * 40}
            for index in range(CANDIDATE_LIMIT)
        ]
        incoming = [{"slug": "query", "title": "query", "definition": "새 개념"}]
        jev = PairJev()

        _resolver(jev).resolve(incoming, existing)

        self.assertGreater(len(jev.requests), 1)
        self.assertEqual(sorted(jev.sent("query")), [concept["slug"] for concept in existing])
        for request in jev.requests:
            self.assertLessEqual(request_tokens(request), MAX_BATCH_TOKENS)

    def test_exact_name_merges_without_jev_or_embedding(self) -> None:
        incoming = [{"slug": "memo-new", "title": "메모", "aliases": [], "definition": "결과를 저장한다."}]
        jev, embedder = PairJev(), AngleEmbedder()

        result = _resolver(jev, embedder).resolve(incoming, EXISTING)

        self.assertEqual(
            result["resolutions"],
            [{"incoming_slug": "memo-new", "decision": "merge_into", "canonical_slug": "memoization", "alias_to_add": None}],
        )
        self.assertEqual(jev.requests, [])
        self.assertEqual(embedder.calls, [])

    def test_reuses_cached_embeddings_across_ingests(self) -> None:
        embedder = AngleEmbedder()

        _resolver(PairJev(), embedder).resolve(INCOMING, EXISTING)
        _resolver(PairJev(), embedder).resolve(INCOMING, EXISTING)

        self.assertEqual(len(embedder.calls), 1)

    def test_embedding_failure_uses_existing_resolver_result(self) -> None:
        fallback = RecordingResolver()
        jev = PairJev()

        result = _resolver(jev, FailingEmbedder(), fallback).resolve(INCOMING, EXISTING, [])

        self.assertEqual(result["resolutions"][0]["incoming_slug"], "llm")
        self.assertEqual(jev.requests, [])
        self.assertEqual(len(fallback.calls), 1)

    def test_hints_use_existing_resolver_hint_result_only(self) -> None:
        fallback = RecordingResolver()
        hints = [{"slug": "hint"}]

        result = _resolver(PairJev(), fallback=fallback).resolve(INCOMING, EXISTING, hints)

        self.assertEqual(result["resolutions"], [])
        self.assertEqual(result["hint_resolutions"][0]["hint_slug"], "hint")
        self.assertEqual(fallback.calls, [(INCOMING, EXISTING, hints)])

    def test_unavailable_jev_uses_existing_resolver_result(self) -> None:
        fallback = RecordingResolver()

        result = _resolver(FailingJev(), fallback=fallback).resolve(INCOMING, EXISTING, [])

        self.assertEqual(result["resolutions"][0]["incoming_slug"], "llm")
        self.assertEqual(len(fallback.calls), 1)

    def test_drops_merge_into_incoming_concept_that_merges_elsewhere(self) -> None:
        incoming = [
            {"slug": "a", "title": "평균", "definition": "합을 개수로 나눈 값이다."},
            {"slug": "b", "title": "산술평균", "definition": "합을 개수로 나눈 값이다."},
        ]
        existing = [{"slug": "mean", "title": "mean", "aliases": [], "summary": "합을 개수로 나눈 값이다."}]
        jev = PairJev({("a", "b"): ("same", {"same": 0.9}), ("b", "mean"): ("same", {"same": 0.9})})

        result = _resolver(jev).resolve(incoming, existing)

        self.assertEqual([item["incoming_slug"] for item in result["resolutions"]], ["b"])


class WithJevConceptMergeTest(unittest.TestCase):
    def test_keeps_existing_resolver_when_disabled(self) -> None:
        resolver = RecordingResolver()
        with patch.dict("os.environ", {"JEV_CONCEPT_MERGE_ENABLED": "false", "TYPESAFE_API_KEY": "key"}):
            self.assertIs(with_jev_concept_merge(resolver), resolver)

    def test_wraps_resolver_when_enabled_with_key_and_embedding_server(self) -> None:
        env = {"JEV_CONCEPT_MERGE_ENABLED": "true", "TYPESAFE_API_KEY": "key", "EMBEDDING_SERVICE_URL": "http://embedding"}
        with (
            patch.dict("os.environ", env),
            patch.object(jev_concept_resolver, "_remote_embedding_model", return_value=AngleEmbedder()),
        ):
            self.assertIsInstance(with_jev_concept_merge(RecordingResolver()), JevConceptResolver)

    def test_keeps_existing_resolver_without_embedding_server(self) -> None:
        # 편입 워커가 BGE-M3를 직접 올리지 않도록 임베딩 서버가 없거나 설정이 잘못되면 Jev 병합을 쓰지 않는다.
        resolver = RecordingResolver()
        env = {"JEV_CONCEPT_MERGE_ENABLED": "true", "TYPESAFE_API_KEY": "key"}
        with patch.dict("os.environ", env), patch.dict("os.environ", {"EMBEDDING_SERVICE_URL": ""}):
            self.assertIs(with_jev_concept_merge(resolver), resolver)
        with (
            patch.dict("os.environ", {**env, "EMBEDDING_SERVICE_URL": "http://embedding"}),
            patch.object(jev_concept_resolver, "_remote_embedding_model", side_effect=RuntimeError("no token")),
        ):
            self.assertIs(with_jev_concept_merge(resolver), resolver)


if __name__ == "__main__":
    unittest.main()
