"""재편입의 참조 정리가 이번 편입이 이미 쓴 다른 페이지를 이번 기여 없는 이전 상태로 되돌리지 않는다."""
from types import SimpleNamespace

from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as repository
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_output_persistence as persistence


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    """쿼리 순서대로 준비한 결과를 돌려준다."""

    def __init__(self, *results):
        self._results = list(results)

    def execute(self, sql, params=None):
        return _Rows(self._results.pop(0))


def _ledger(page_id):
    return [{"page_id": page_id, "active": True, "ingest_operation_id": "op1", "sequence_revision": 1}]


def _loaded(page_id, contribution):
    return [PageContribution(page_id, "concept", "op1", 1, "k1", contribution)]


def test_prune_replays_pending_contribution_of_this_operation(monkeypatch) -> None:
    # B는 이번 편입(op2)이 기여했고, 다시 그린 A의 사라진 하위 항목을 가리킨다.
    replayed: list[list[PageContribution]] = []
    dangling = {"relation": repository.ENTRY_REFERENCE, "target": "concept:a", "entry_id": "entry:gone"}

    def rebuild(contributions):
        replayed.append(contributions)
        return SimpleNamespace(
            deleted=False, markdown="# B", title="B", summary=None, supported_links=[dangling],
            source_document_ids=["doc"], entries=[],
        )

    monkeypatch.setattr(repository, "read_contributions", lambda page_ids, workspace_id: _ledger("B"))
    monkeypatch.setattr(repository, "load_concept_contributions", lambda **kwargs: _loaded("B", {"operation_id": "op1"}))
    monkeypatch.setattr(repository, "rebuild_concept_page", rebuild)
    monkeypatch.setattr(repository, "write_text_object", lambda *args: None)
    monkeypatch.setattr(repository, "read_text_object", lambda uri: "# B")
    monkeypatch.setattr(repository, "render_entry_index", lambda markdown, entries, links: markdown)
    monkeypatch.setattr(repository, "_apply_restored_wiki_state", lambda *args, **kwargs: set())

    pending = {"B": {"operation_id": "op2", "page_id": "B", "evidence_units": [{"claim": "이번 편입 근거"}]}}
    changed: list[dict] = [{"page_id": "A", "entries": [], "supported_links": []}]
    repository._prune_dangling_references(_Conn([], [{"page_id": "B"}]), "ws", "op2", changed, [], pending)

    last = replayed[0][-1]
    assert (last.operation_id, last.sequence, last.contribution) == ("op2", 2, pending["B"])
    assert [page["page_id"] for page in changed] == ["A", "B"]

    # 이번 작업 기여가 없는 페이지(복구 경로)는 원장 그대로 재생한다.
    replayed.clear()
    repository._prune_dangling_references(_Conn([], [{"page_id": "B"}]), "ws", "op2", [{"page_id": "A"}], [])
    assert [item.operation_id for item in replayed[0]] == ["op1"]


def test_rebuild_invalidated_pages_passes_this_operation_contributions_to_prune(monkeypatch) -> None:
    # A는 무효화 블록을 인용해 다시 그리고, B는 이번 편입이 기여했지만 무효화 블록은 인용하지 않는다.
    stale = {"concept": {"anchor_reference_ids": ["doc:B5"]}}
    fresh = {"concept": {"anchor_reference_ids": ["doc:B1"]}}
    monkeypatch.setattr(
        persistence, "read_contributions",
        lambda page_ids, workspace_id: [*_ledger("A"), *_ledger("B")],
    )
    monkeypatch.setattr(
        persistence, "load_concept_contributions",
        lambda **kwargs: _loaded(kwargs["page_id"], stale if kwargs["page_id"] == "A" else fresh),
    )
    monkeypatch.setattr(
        persistence, "rebuild_concept_page",
        lambda contributions: SimpleNamespace(
            deleted=False, markdown="# A", supported_links=[], source_document_ids=["doc"], entries=[],
        ),
    )
    monkeypatch.setattr(persistence, "write_text_object", lambda *args: None)
    monkeypatch.setattr(persistence, "read_text_object", lambda uri: "# A")
    received = {}

    def prune(conn, workspace_id, operation_id, applied, deleted_ids, pending):
        received.update(applied=[page["page_id"] for page in applied], pending=pending)

    manifest = {
        "concept_contributions": {"a": {"operation_id": "op2"}, "b": {"operation_id": "op2", "evidence_units": []}},
        "concept_pages": [{"slug": "a", "markdown": "# A"}, {"slug": "b", "markdown": "# B"}],
    }
    persistence._rebuild_invalidated_pages(
        _Conn([]), "doc", "ws", "op2", ["doc:B5"], manifest, {"a": "A", "b": "B"}, [],
        lambda *args: None, prune, None,
    )

    assert received["applied"] == ["A"]
    assert received["pending"]["B"] == {"operation_id": "op2", "evidence_units": [], "page_id": "B"}
