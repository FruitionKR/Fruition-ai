from app.modules.wiki_ingestion.application.concept_contribution import (
    build_concept_contributions,
)
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution
from app.modules.wiki_ingestion.infrastructure.concept_contribution_rebuild import (
    rebuild_concept_page,
)


def test_builds_only_current_ingest_concept_inputs_and_links() -> None:
    normalized = {
        "document": {"document_id": "doc-A"},
        "concept_ledger": [
            {
                "slug": "shared",
                "title": "공유 개념",
                "definition": "A2에서 추출한 정의",
                "anchor_reference_ids": ["B0002"],
                "evidence_claim_ids": ["ev-A2"],
                "source_document_ids": ["doc-A"],
            }
        ],
        "evidence_units": [
            {
                "evidence_id": "ev-A2",
                "claim": "A2의 근거",
                "anchor_reference_ids": ["B0002"],
                "source_document_id": "doc-A",
            },
            {
                "evidence_id": "ev-old",
                "claim": "누적 normalized에만 있던 과거 근거",
                "anchor_reference_ids": ["B0001"],
                "source_document_id": "doc-A",
            },
        ],
    }
    source_blocks = [
        {"document_id": "doc-A", "block_id": "B0001", "text": "과거 블록"},
        {"document_id": "doc-A", "block_id": "B0002", "text": "A2 블록"},
    ]
    links = [
        {
            "source": "source:doc-A",
            "target": "concept:shared",
            "relation": "source_mentions_concept",
        },
        {
            "source": "source:doc-A",
            "target": "concept:unrelated",
            "relation": "source_mentions_concept",
        },
    ]

    contributions = build_concept_contributions(
        operation_id="op-A2",
        normalized=normalized,
        source_blocks=source_blocks,
        links=links,
        source_key_points=[
            {
                "text": "A2에서 추출한 핵심 포인트",
                "anchor_reference_ids": ["B0002"],
            }
        ],
    )

    assert list(contributions) == ["shared"]
    contribution = contributions["shared"]
    assert contribution["operation_id"] == "op-A2"
    assert [item["evidence_id"] for item in contribution["evidence_units"]] == [
        "op-A2:ev-A2"
    ]
    assert contribution["concept"]["evidence_claim_ids"] == ["op-A2:ev-A2"]
    assert contribution["concept"]["anchor_reference_ids"] == [
        "doc-A:B0002"
    ]
    assert [item["block_id"] for item in contribution["source_blocks"]] == [
        "B0002"
    ]
    assert contribution["links"] == [links[0]]
    assert contribution["source_key_points"] == [
        {
            "text": "A2에서 추출한 핵심 포인트",
            "anchor_reference_ids": ["doc-A:B0002"],
        }
    ]


def test_keeps_edge_support_for_related_concept_generation_input() -> None:
    relation = {
        "source": "concept:shared",
        "target": "concept:target",
        "relation": "supports_or_enables",
    }

    contribution = build_concept_contributions(
        operation_id="op-B",
        normalized={
            "document": {"document_id": "doc-B"},
            "concept_ledger": [
                {
                    "slug": "shared",
                    "title": "공유 개념",
                    "evidence_claim_ids": [],
                }
            ],
            "evidence_units": [],
        },
        source_blocks=[],
        links=[relation],
    )["shared"]

    assert contribution["links"] == [relation]


def test_builds_contribution_for_existing_concept_update_decision() -> None:
    contribution = build_concept_contributions(
        operation_id="op-B",
        normalized={
            "document": {"document_id": "doc-B"},
            "concept_ledger": [],
            "existing_concept_index": [
                {
                    "slug": "existing",
                    "title": "기존 개념",
                    "definition": "기존 정의",
                }
            ],
            "evidence_units": [],
        },
        source_blocks=[
            {
                "document_id": "doc-B",
                "block_id": "B0001",
                "text": "새 근거 원문",
            }
        ],
        links=[],
        concept_update_decisions=[
            {
                "decision": "same_concept",
                "concept_slug": "existing",
                "claim_id": "claim-1",
                "claim": "기존 개념에 추가할 근거",
                "refs": ["doc-B:B0001"],
            }
        ],
    )["existing"]

    assert contribution["concept"]["slug"] == "existing"
    assert contribution["concept"]["definition"] == ""
    assert contribution["concept"]["evidence_claim_ids"] == [
        "op-B:claim-1"
    ]
    assert contribution["evidence_units"][0]["claim"] == (
        "기존 개념에 추가할 근거"
    )
    assert contribution["evidence_units"][0]["anchor_reference_ids"] == [
        "doc-B:B0001"
    ]
    assert contribution["source_blocks"][0]["block_id"] == "B0001"


def test_default_key_points_come_from_current_contribution_normalized() -> None:
    contribution = build_concept_contributions(
        operation_id="op-A2",
        normalized={
            "document": {"document_id": "doc-A"},
            "semantic_notes": [
                {
                    "key_points": [
                        {
                            "text": "A2 현재 ingest 핵심",
                            "anchor_reference_ids": ["B0002"],
                        }
                    ]
                }
            ],
            "concept_ledger": [
                {
                    "slug": "shared",
                    "anchor_reference_ids": ["B0002"],
                    "evidence_claim_ids": [],
                }
            ],
            "evidence_units": [],
        },
        source_blocks=[
            {
                "document_id": "doc-A",
                "block_id": "B0002",
                "text": "A2 블록",
            }
        ],
        links=[],
    )["shared"]

    assert contribution["source_key_points"] == [
        {
            "text": "A2 현재 ingest 핵심",
            "anchor_reference_ids": ["doc-A:B0002"],
        }
    ]


def test_saved_generation_input_rebuilds_evidence_and_source_key_points() -> None:
    link = {
        "source": "source:doc-A",
        "target": "concept:shared",
        "relation": "source_mentions_concept",
    }
    contribution = build_concept_contributions(
        operation_id="op-A",
        normalized={
            "document": {"document_id": "doc-A"},
            "concept_ledger": [
                {
                    "slug": "shared",
                    "title": "공유 개념",
                    "definition": "추출된 정의",
                    "anchor_reference_ids": ["B0001"],
                    "display_reference_ids": ["B0001"],
                    "evidence_claim_ids": ["ev-A"],
                    "source_document_ids": ["doc-A"],
                }
            ],
            "evidence_units": [
                {
                    "evidence_id": "ev-A",
                    "claim": "추출된 근거",
                    "anchor_reference_ids": ["B0001"],
                    "related_concept_slugs": ["shared"],
                    "source_document_id": "doc-A",
                }
            ],
        },
        source_blocks=[
            {
                "document_id": "doc-A",
                "block_id": "B0001",
                "text": "원문 블록",
            }
        ],
        source_key_points=[
            {
                "text": "생성에 전달된 핵심 포인트",
                "anchor_reference_ids": ["B0001"],
            }
        ],
        links=[link],
    )["shared"]
    contribution["page_id"] = "C3"

    rebuilt = rebuild_concept_page(
        [
            PageContribution(
                page_id="C3",
                page_type="concept",
                operation_id="op-A",
                sequence=1,
                markdown_key="wiki/ws/pages/C3/ops/op-A.md",
                contribution=contribution,
            )
        ]
    )

    assert "추출된 정의" in rebuilt.markdown
    assert "추출된 근거" in rebuilt.markdown
    assert "생성에 전달된 핵심 포인트" in rebuilt.markdown
    assert rebuilt.supported_links == (link,)


def test_rebuild_preserves_same_local_ids_from_different_documents() -> None:
    contributions = []
    for sequence, document_id in enumerate(("doc-A", "doc-B"), start=1):
        operation_id = f"op-{document_id}"
        contribution = {
            "operation_id": operation_id,
            "document_id": document_id,
            "page_id": "C3",
            "concept": {
                "slug": "shared",
                "title": "공유 개념",
                "definition": f"{document_id} 정의",
                "anchor_reference_ids": ["B0001"],
                "display_reference_ids": ["B0001"],
                "evidence_claim_ids": ["ev_0001"],
                "source_document_ids": [document_id],
            },
            "evidence_units": [
                {
                    "evidence_id": "ev_0001",
                    "claim": f"{document_id} 근거",
                    "anchor_reference_ids": ["B0001"],
                    "related_concept_slugs": ["shared"],
                    "document_id": document_id,
                    "source_document_id": document_id,
                }
            ],
            "source_key_points": [
                {
                    "text": f"{document_id} 핵심",
                    "anchor_reference_ids": ["B0001"],
                }
            ],
            "source_blocks": [],
            "links": [],
        }
        contributions.append(
            PageContribution(
                page_id="C3",
                page_type="concept",
                operation_id=operation_id,
                sequence=sequence,
                markdown_key=f"wiki/ws/pages/C3/ops/{operation_id}.md",
                contribution=contribution,
            )
        )

    rebuilt = rebuild_concept_page(contributions)

    assert "doc-A 근거" in rebuilt.markdown
    assert "doc-B 근거" in rebuilt.markdown
    assert "doc-A 핵심 [doc-A:B0001]" in rebuilt.markdown
    assert "doc-B 핵심 [doc-B:B0001]" in rebuilt.markdown
