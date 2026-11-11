from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.core.jev_client import JevUnavailable
from app.modules.mcp_read.application.wiki_search import SearchWikiUseCase, WikiSearchError
from app.modules.mcp_read.infrastructure.jev_wiki_search_evidence import JevWikiSearchEvidence
from app.modules.query.infrastructure.jev_evidence_selector import EvidenceCandidate
from tests.modules.mcp_read.test_wiki_search import Repository, Search, candidate, limits, service
from tests.modules.mcp_read.test_wiki_search import request as plain_request


def request(**kwargs):
    """이 파일의 검색은 document가 Jev 비용을 허용한 요청이다."""
    return plain_request(**{"jev_allowed": True, **kwargs})


def unit_candidate(page_id: str, text: str, block_id: str = "B0001", document_id: str = "doc-a"):
    return candidate(page_id, text=text, block_id=block_id, document_id=document_id)


def shared_candidate(page_ids: tuple[str, ...], text: str, block_id: str = "B0001"):
    """본문·블록·문서가 같은 unit 묶음. 웹 후보처럼 여러 page를 가리킨다."""
    first, _ = candidate(page_ids[0], text=text, block_id=block_id)
    shared = EvidenceCandidate(unit=first.unit, source_titles=first.source_titles, page_ids=page_ids)
    return [(shared, candidate(page_id, text=text, block_id=block_id)[1]) for page_id in page_ids]


class Jev:
    """후보 text에 keyword가 있으면 include로 답하고 받은 후보를 기록한다."""

    def __init__(self, keyword: str = "refund", *, fail: bool = False) -> None:
        self.keyword = keyword
        self.fail = fail
        self.states: list[dict] = []

    def choose(self, state: str, questions: dict) -> dict:
        if self.fail:
            raise JevUnavailable("down")
        decoded = json.loads(state)
        self.states.append(decoded)
        texts = {item["id"]: item["text"] for item in decoded["candidates"]}
        return {
            key: {
                "choice": "include" if self.keyword in texts[key] else "exclude",
                "probabilities": {"include": 0.9 if texts[key].startswith(self.keyword) else 0.6},
            }
            for key in questions
        }


def with_selector(repository: Repository, selector) -> SearchWikiUseCase:
    return SearchWikiUseCase(
        repository=repository,
        embedding_search=Search(),
        limits=limits(),
        cursor_key=b"cursor-test-key-that-is-at-least-32-bytes",
        query_embedding_model="test-embedding",
        evidence_selector=selector,
    )


def with_jev(repository: Repository, jev: Jev, *, max_selected: int = 8) -> SearchWikiUseCase:
    return with_selector(repository, JevWikiSearchEvidence(jev, max_selected=max_selected))


def pages(use_case, *, page_size: int = 1) -> list[str]:
    ids: list[str] = []
    cursor = None
    while True:
        response = use_case.execute(request(cursor=cursor, page_size=page_size))
        ids.extend(item["page_id"] for item in response["results"])
        cursor = response["next_cursor"]
        if cursor is None:
            return ids


CANDIDATES = [
    unit_candidate("page-a", "query alpha query", block_id="B0001"),
    unit_candidate("page-b", "query beta", block_id="B0002"),
    unit_candidate("page-c", "query refund later", block_id="B0003"),
    unit_candidate("page-d", "refund query first", block_id="B0004"),
]


def test_selected_first_then_hybrid_and_continuation_uses_frozen_order_without_jev() -> None:
    hybrid = pages(service(Repository(list(CANDIDATES)), search_limits=limits()))
    jev = Jev()
    use_case = with_jev(Repository(list(CANDIDATES)), jev)

    ordered = pages(use_case)

    assert hybrid == ["page-a", "page-b", "page-c", "page-d"]
    assert ordered == ["page-d", "page-c", "page-a", "page-b"]
    # 네 페이지를 넘기는 동안 Jev는 첫 요청에서 한 번만 불렸다.
    assert len(jev.states) == 1
    assert sorted(item["id"] for item in jev.states[0]["candidates"]) == [f"unit-{page.page_id}" for _, page in CANDIDATES]


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"content_hash": "sha256:" + "b" * 64},
        {"block_id": "B0009"},
    ],
)
def test_continuation_rejects_changed_version_hash_or_evidence(change: dict) -> None:
    repository = Repository(list(CANDIDATES))
    jev = Jev()
    use_case = with_jev(repository, jev)
    cursor = use_case.execute(request())["next_cursor"]
    repository.candidates[1] = candidate("page-b", text="query beta", **{"block_id": "B0002", **change})

    with pytest.raises(WikiSearchError) as exc:
        use_case.execute(request(cursor=cursor))

    assert exc.value.code == "STALE_CURSOR"
    assert len(jev.states) == 1


def test_continuation_rejects_changed_candidate_set() -> None:
    repository = Repository(list(CANDIDATES))
    use_case = with_jev(repository, Jev())
    cursor = use_case.execute(request())["next_cursor"]
    repository.candidates[0] = unit_candidate("page-new", "query alpha query")

    with pytest.raises(WikiSearchError) as exc:
        use_case.execute(request(cursor=cursor))

    assert exc.value.code == "STALE_CURSOR"


def test_unavailable_or_disabled_jev_keeps_hybrid_order() -> None:
    hybrid = pages(service(Repository(list(CANDIDATES)), search_limits=limits()))

    assert pages(with_jev(Repository(list(CANDIDATES)), Jev(fail=True))) == hybrid


def test_jev_sees_web_candidates_and_shared_evidence_moves_its_pages_together() -> None:
    repository = Repository([
        unit_candidate("page-x", "query unrelated", block_id="B0009"),
        # 같은 본문·블록·문서는 웹 후보에서 하나로 묶여 있고, 고르면 묶인 page가 함께 앞으로 간다.
        *shared_candidate(("page-a", "page-b"), "refund policy", block_id="B0001"),
        # 이미 고른 블록만 가리키면 건너뛴다.
        unit_candidate("page-c", "refund again", block_id="B0001"),
    ])
    jev = Jev()
    use_case = with_jev(repository, jev, max_selected=1)

    response = use_case.execute(request(page_size=2))

    assert [item["page_id"] for item in response["results"]] == ["page-a", "page-b"]
    assert response["results"][1]["snippet"] == "refund policy"
    [state] = jev.states
    assert [(item["id"], item["page_ids"]) for item in sorted(state["candidates"], key=lambda item: item["id"])] == [
        ("unit-page-a", ["page-a", "page-b"]),
        ("unit-page-c", ["page-c"]),
        ("unit-page-x", ["page-x"]),
    ]
    call = repository.calls[0]
    assert (call["candidate_limit"], call["scan_page_limit"], call["scan_unit_limit"]) == (4, 20, 100)


def test_jev_selected_unit_becomes_the_page_snippet_and_survives_continuation() -> None:
    first, page_a = candidate("page-a", text="query alpha", block_id="B0001")
    second, _ = candidate("page-b", text="query beta", block_id="B0002")
    # page-a의 두 번째 unit이 Jev에 뽑힌다. 하이브리드 대표 unit은 첫 번째 unit이다.
    selected = EvidenceCandidate(
        unit=replace(first.unit, id="unit-page-a-refund", text="refund for page a", source_block_ids=["B0003"]),
        source_titles=first.source_titles,
        page_ids=("page-a",),
    )
    repository = Repository([(second, candidate("page-b")[1]), (first, page_a), (selected, page_a)])
    jev = Jev()
    use_case = with_jev(repository, jev)

    head = use_case.execute(request())
    tail = use_case.execute(request(cursor=head["next_cursor"]))

    assert head["results"][0]["page_id"] == "page-a"
    assert head["results"][0]["snippet"] == "refund for page a"
    assert head["results"][0]["evidence"] == [{"document_id": "doc-a", "block_id": "B0003"}]
    assert tail["results"][0]["page_id"] == "page-b"
    assert len(jev.states) == 1


def test_selector_ids_outside_candidates_are_ignored() -> None:
    class Inventing:
        def select(self, _actor, _query, _candidates):
            return ["unit-unknown", "unit-page-b", "unit-page-b"]

    use_case = with_selector(Repository([candidate("page-a"), candidate("page-b")]), Inventing())

    response = use_case.execute(request(page_size=2))

    assert [item["page_id"] for item in response["results"]] == ["page-b", "page-a"]
    assert response["results"][0]["evidence"] == [{"document_id": "doc-a", "block_id": "B0001"}]


def test_jev_not_allowed_keeps_hybrid_order_without_calling_selector() -> None:
    class Forbidden:
        def select(self, _actor, _query, _candidates):
            pytest.fail("jev_allowed=false must not call the Jev selector")

    hybrid = pages(service(Repository(list(CANDIDATES)), search_limits=limits()))
    use_case = with_selector(Repository(list(CANDIDATES)), Forbidden())

    ordered: list[str] = []
    cursor = None
    while True:
        response = use_case.execute(plain_request(cursor=cursor))
        ordered.extend(item["page_id"] for item in response["results"])
        cursor = response["next_cursor"]
        if cursor is None:
            break

    assert ordered == hybrid


def test_continuation_accepts_a_different_jev_flag_and_keeps_frozen_order() -> None:
    jev = Jev()
    use_case = with_jev(Repository(list(CANDIDATES)), jev)
    first = use_case.execute(request())

    second = use_case.execute(plain_request(cursor=first["next_cursor"]))

    assert first["results"][0]["page_id"] == "page-d"
    assert second["results"][0]["page_id"] == "page-c"
    assert len(jev.states) == 1
