import json
import unittest
from threading import Lock

from app.core.jev_client import JevUnavailable
from app.modules.query.domain.entities import EvidenceSnippet, RetrievedPage, WikiEmbeddingUnit, WikiPage
from app.modules.query.infrastructure.jev_evidence_selector import JevEvidenceSelector


class UnitRepository:
    def __init__(self, units: list[WikiEmbeddingUnit]) -> None:
        self._units = units

    def list_workspace_embedding_units(self, workspace_id: str) -> list[WikiEmbeddingUnit]:
        return self._units


class TextOverlapSearch:
    def score(self, query: str, documents: list[str]) -> list[float]:
        return [1.0 if "환불" in document else 0.1 for document in documents]


class RecordingFallback:
    def __init__(self) -> None:
        self.calls = 0

    def select(self, question, related_pages, embedding_units_by_page_id, workspace_id=None):
        self.calls += 1
        return [EvidenceSnippet(rank=1, source_document_id="doc-fallback", source_block_ids=["B0001"], text="기존 근거")]


class IncludingJev:
    """후보 text에 키워드가 있으면 include로 답하고 호출 묶음 크기를 기록한다."""

    def __init__(self, keyword: str) -> None:
        self._keyword = keyword
        self._lock = Lock()
        self.batch_sizes: list[int] = []

    def choose(self, state: str, questions: dict) -> dict:
        candidates = {item["id"]: item["text"] for item in json.loads(state)["candidates"]}
        with self._lock:
            self.batch_sizes.append(len(questions))
        answers = {}
        for key in questions:
            include = self._keyword in candidates[key]
            probability = 0.9 if candidates[key].startswith("환불 기한") else 0.6
            answers[key] = {
                "choice": "include" if include else "exclude",
                "probabilities": {"include": probability if include else 0.1},
            }
        return answers


class FailingJev:
    def choose(self, state: str, questions: dict) -> dict:
        raise JevUnavailable("Jev HTTP 402")


def _unit(unit_id: str, text: str, block: str = "B0001") -> WikiEmbeddingUnit:
    return WikiEmbeddingUnit(unit_id, "source:doc", "doc", "evidence", [block], text)


def _selector(jev, units, fallback=None, **kwargs) -> tuple[JevEvidenceSelector, RecordingFallback]:
    fallback = fallback or RecordingFallback()
    search = TextOverlapSearch()
    return JevEvidenceSelector(jev, fallback, UnitRepository(units), search, search, **kwargs), fallback


class JevEvidenceSelectorTest(unittest.TestCase):
    def test_returns_included_units_by_probability(self) -> None:
        units = [
            _unit("u1", "환불 신청은 앱에서 한다.", "B0001"),
            _unit("u2", "배송은 3일 걸린다.", "B0002"),
            _unit("u3", "환불 기한은 7일이다.", "B0003"),
        ]
        selector, fallback = _selector(IncludingJev("환불"), units)

        snippets = selector.select("환불 기한은?", [], {}, workspace_id="ws")

        self.assertEqual([snippet.text for snippet in snippets], ["환불 기한은 7일이다.", "환불 신청은 앱에서 한다."])
        self.assertEqual([snippet.rank for snippet in snippets], [1, 2])
        self.assertEqual(snippets[0].source_block_ids, ["B0003"])
        self.assertEqual(fallback.calls, 0)

    def test_no_included_unit_returns_empty_evidence(self) -> None:
        selector, fallback = _selector(IncludingJev("없는 단어"), [_unit("u1", "배송은 3일 걸린다.")])

        self.assertEqual(selector.select("환불 기한은?", [], {}, workspace_id="ws"), [])
        self.assertEqual(fallback.calls, 0)

    def test_unavailable_jev_uses_existing_selector(self) -> None:
        selector, fallback = _selector(FailingJev(), [_unit("u1", "환불 기한은 7일이다.")])

        snippets = selector.select("환불 기한은?", [], {}, workspace_id="ws")

        self.assertEqual(snippets[0].text, "기존 근거")
        self.assertEqual(fallback.calls, 1)

    def test_web_context_uses_existing_selector(self) -> None:
        jev = IncludingJev("환불")
        selector, fallback = _selector(jev, [_unit("u1", "환불 기한은 7일이다.")])
        web_page = RetrievedPage(page=WikiPage(id="web:1", page_type="web", title="웹", slug="web", summary=""), score=1.0, role="web")

        selector.select("환불 기한은?", [web_page], {}, workspace_id="ws")

        self.assertEqual(jev.batch_sizes, [])
        self.assertEqual(fallback.calls, 1)

    def test_limits_candidates_and_batch_size(self) -> None:
        units = [_unit(f"u{index:03}", f"환불 근거 {index}", f"B{index:04}") for index in range(1, 11)]
        jev = IncludingJev("환불")
        selector, _ = _selector(jev, units, candidate_limit=7, max_batch_candidates=3)

        snippets = selector.select("환불", [], {}, workspace_id="ws")

        self.assertEqual(sorted(jev.batch_sizes), [1, 3, 3])
        self.assertEqual(len(snippets), 7)

    def test_reuses_selection_for_same_question_in_one_query(self) -> None:
        jev = IncludingJev("환불")
        selector, _ = _selector(jev, [_unit("u1", "환불 기한은 7일이다.")])

        first = selector.select("환불 기한은?", [], {}, workspace_id="ws")
        second = selector.select("환불 기한은?", [], {}, workspace_id="ws")

        self.assertEqual(first, second)
        self.assertEqual(jev.batch_sizes, [1])

    def test_skips_units_without_valid_source_reference(self) -> None:
        jev = IncludingJev("환불")
        selector, fallback = _selector(jev, [_unit("u1", "환불 기한은 7일이다.", "not-a-block")])

        selector.select("환불 기한은?", [], {}, workspace_id="ws")

        self.assertEqual(jev.batch_sizes, [])
        self.assertEqual(fallback.calls, 1)


if __name__ == "__main__":
    unittest.main()
