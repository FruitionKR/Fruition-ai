from __future__ import annotations

import json

from app.modules.wiki_ingestion.domain.operation_recovery import (
    PageContribution,
    plan_page_recovery,
)
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    ConceptContributionError,
    load_concept_contributions,
    rebuild_concept_page,
)


def _contribution(
    page_id: str,
    operation_id: str,
    sequence: int,
    *,
    page_type: str = "concept",
    document_id: str | None = None,
    evidence: str | None = None,
    links: list[dict[str, str]] | None = None,
) -> PageContribution:
    payload = None
    if page_type == "concept":
        source_document_id = document_id or operation_id.lower()
        evidence_id = f"ev-{operation_id}"
        payload = {
            "schema_version": 1,
            "operation_id": operation_id,
            "document_id": source_document_id,
            "page_id": page_id,
            "concept": {
                "slug": page_id.lower(),
                "title": page_id,
                "definition": evidence or f"{operation_id} 정의",
                "source_document_ids": [source_document_id],
                "evidence_claim_ids": [evidence_id],
            },
            "evidence_units": [
                {
                    "evidence_id": evidence_id,
                    "claim": evidence or f"{operation_id} 근거",
                    "anchor_reference_ids": [f"B{sequence:04d}"],
                    "related_concept_slugs": [page_id.lower()],
                    "source_document_id": source_document_id,
                }
            ],
            "source_blocks": [
                {
                    "document_id": source_document_id,
                    "block_id": f"B{sequence:04d}",
                    "text": evidence or f"{operation_id} 원문",
                }
            ],
            "links": links or [],
        }
    return PageContribution(
        page_id=page_id,
        page_type=page_type,
        operation_id=operation_id,
        sequence=sequence,
        markdown_key=f"wiki/ws/pages/{page_id}/ops/{operation_id}.md",
        contribution=payload,
    )


def _history() -> dict[str, list[PageContribution]]:
    return {
        "S_A": [
            _contribution("S_A", "A", 1, page_type="source"),
            _contribution("S_A", "A2", 4, page_type="source"),
        ],
        "C1": [_contribution("C1", "A", 1)],
        "C2": [
            _contribution("C2", "A", 1),
            _contribution("C2", "B", 2),
            _contribution("C2", "A2", 4),
        ],
        "C3": [
            _contribution("C3", "A", 1),
            _contribution("C3", "B", 2),
            _contribution("C3", "C", 3),
            _contribution("C3", "A2", 4),
            _contribution("C3", "D", 5),
        ],
        "C4": [
            _contribution("C4", "A", 1),
            _contribution("C4", "C", 3),
        ],
        "C5": [
            _contribution("C5", "A", 1),
            _contribution("C5", "A2", 4),
        ],
        "C6": [
            _contribution("C6", "A2", 4),
            _contribution("C6", "D", 5),
        ],
    }


def _plans(excluded_operations: set[str]) -> dict[str, tuple[str, tuple[str, ...]]]:
    return {
        page_id: (
            plan.action,
            tuple(item.operation_id for item in plan.kept_contributions),
        )
        for page_id, contributions in _history().items()
        if (plan := plan_page_recovery(contributions, excluded_operations)).action
        != "unchanged"
    }


def test_recent_ingest_uses_previous_snapshots_without_rebuild() -> None:
    assert _plans({"D"}) == {
        "C3": ("restore", ("A", "B", "C", "A2")),
        "C6": ("restore", ("A2",)),
    }


def test_reingest_removal_rebuilds_pages_that_have_later_contributions() -> None:
    assert _plans({"A2"}) == {
        "S_A": ("restore", ("A",)),
        "C2": ("restore", ("A", "B")),
        "C3": ("rebuild", ("A", "B", "C", "D")),
        "C5": ("restore", ("A",)),
        "C6": ("rebuild", ("D",)),
    }


def test_middle_ingest_removals_keep_only_unexcluded_contributions() -> None:
    assert _plans({"C"}) == {
        "C3": ("rebuild", ("A", "B", "A2", "D")),
        "C4": ("restore", ("A",)),
    }
    assert _plans({"B"}) == {
        "C2": ("rebuild", ("A", "A2")),
        "C3": ("rebuild", ("A", "C", "A2", "D")),
    }


def test_original_ingest_removal_also_removes_its_reingest() -> None:
    assert _plans({"A", "A2"}) == {
        "S_A": ("delete", ()),
        "C1": ("delete", ()),
        "C2": ("rebuild", ("B",)),
        "C3": ("rebuild", ("B", "C", "D")),
        "C4": ("rebuild", ("C",)),
        "C5": ("delete", ()),
        "C6": ("rebuild", ("D",)),
    }


def test_rebuild_selects_remaining_json_and_supported_links() -> None:
    shared_link = {
        "source": "concept:c3",
        "target": "concept:shared",
        "relation": "related_to",
    }
    removed_link = {
        "source": "concept:c3",
        "target": "concept:reingest-only",
        "relation": "related_to",
    }
    contributions = [
        _contribution(
            "C3",
            "A",
            1,
            document_id="doc-A",
            evidence="A 근거",
            links=[shared_link],
        ),
        _contribution(
            "C3",
            "B",
            2,
            document_id="doc-B",
            evidence="B 근거",
            links=[shared_link],
        ),
        _contribution(
            "C3",
            "A2",
            3,
            document_id="doc-A",
            evidence="A2에서만 생긴 근거",
            links=[removed_link],
        ),
    ]
    plan = plan_page_recovery(contributions, {"A2"})

    rebuilt = rebuild_concept_page(plan.kept_contributions)

    assert rebuilt.operation_ids == ("A", "B")
    assert "A 근거" in rebuilt.markdown
    assert "B 근거" in rebuilt.markdown
    assert "A2에서만 생긴 근거" not in rebuilt.markdown
    assert rebuilt.supported_links == (shared_link,)


def test_rebuild_rejects_missing_json_and_mixed_concepts() -> None:
    missing_json = _contribution("C3", "A", 1, page_type="source")
    mixed = [
        _contribution("C3", "A", 1),
        _contribution("C4", "B", 2),
    ]

    for contributions, message in (
        ([missing_json], "concept contribution JSON"),
        (mixed, "same concept page"),
    ):
        try:
            rebuild_concept_page(contributions)
        except ConceptContributionError as exc:
            assert message in str(exc)
        else:
            raise AssertionError("invalid contribution input must fail")


def test_reingest_rebuild_reads_only_selected_operation_json() -> None:
    shared_link = {
        "source": "concept:c3",
        "target": "concept:shared",
        "relation": "related_to",
    }
    removed_link = {
        "source": "concept:c3",
        "target": "concept:a2-only",
        "relation": "related_to",
    }
    all_contributions = [
        _contribution("C3", "A", 1, evidence="A 근거", links=[shared_link]),
        _contribution("C3", "B", 2, evidence="B 근거", links=[shared_link]),
        _contribution("C3", "A2", 3, evidence="A2 근거", links=[removed_link]),
    ]
    objects = {
        (
            f"wiki/ws-1/pages/C3/ops/{item.operation_id}.json"
        ): json.dumps(item.contribution, ensure_ascii=False)
        for item in all_contributions
    }
    read_keys: list[str] = []

    selected = load_concept_contributions(
        workspace_id="ws-1",
        page_id="C3",
        keep_contributions=[
            {"operation_id": "A", "sequence": 1},
            {"operation_id": "B", "sequence": 2},
        ],
        read_text=lambda key: read_keys.append(key) or objects[key],
    )
    rebuilt = rebuild_concept_page(selected)

    assert read_keys == [
        "wiki/ws-1/pages/C3/ops/A.json",
        "wiki/ws-1/pages/C3/ops/B.json",
    ]
    assert "A2 근거" not in rebuilt.markdown
    assert rebuilt.supported_links == (shared_link,)
