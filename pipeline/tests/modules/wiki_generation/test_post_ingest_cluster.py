from unittest.mock import MagicMock, patch

from app.modules.wiki_generation.infrastructure.post_ingest_cluster import (
    build_post_ingest_cluster_artifact,
)


def test_post_ingest_cluster_judges_only_non_concept_updates() -> None:
    normalized = {
        "document": {"document_id": "doc-1"},
        "section_candidates": [
            {
                "term": "기존 개념 근거",
                "context": "기존 개념을 보강한다.",
                "anchor_reference_ids": ["B0001"],
            },
            {
                "term": "새 후보",
                "context": "새 cluster 후보이다.",
                "anchor_reference_ids": ["B0002"],
            },
        ],
        "mentions": [],
        "unresolved_related_concept_hints": [],
        "evidence_units": [],
    }
    completion = MagicMock()

    with patch(
        "app.modules.wiki_generation.infrastructure.post_ingest_cluster."
        "judge_meaning_cluster_candidates",
        return_value=[],
    ) as judge:
        artifact = build_post_ingest_cluster_artifact(
            completion=completion,
            normalized=normalized,
            existing_active_markdown="",
            user_id="user-1",
            workspace_id="workspace-1",
            concept_update_decisions=[
                {
                    "candidate_id": "cand_001",
                    "decision": "same_concept",
                    "concept_slug": "existing",
                }
            ],
            core_relation_decisions=[],
        )

    assert [item["candidate_id"] for item in judge.call_args.kwargs["candidates"]] == [
        "cand_002"
    ]
    assert artifact["concept_update_decisions"][0]["candidate_id"] == "cand_001"


def test_post_ingest_marks_promotion_by_core_concept_and_per_touched_cluster() -> None:
    existing = """# Active Meaning Clusters

## cluster: back-emf

type: term_cluster
representative: Back EMF

### Evidence Claims
- claim_a_001: Back EMF는 속도에 비례한다. [doc-a:B0001]

## cluster: slot-opening

type: term_cluster
representative: Slot Opening

### Evidence Claims
- claim_a_002: 슬롯 개방은 토크 리플에 영향을 준다. [doc-a:B0002]

### Promotion
status: candidate
source_refs: [doc-a]
reason: 이미 쌓임
"""
    normalized = {
        "document": {"document_id": "doc-b"},
        "section_candidates": [
            {"term": "Cogging Torque", "context": "코깅 토크는 무부하 토크다.", "anchor_reference_ids": ["B0001"]},
            {"term": "Slot Opening", "context": "슬롯 개방 폭을 줄인다.", "anchor_reference_ids": ["B0002"]},
        ],
        "mentions": [],
        "unresolved_related_concept_hints": [],
        "evidence_units": [],
    }

    with patch(
        "app.modules.wiki_generation.infrastructure.post_ingest_cluster.judge_meaning_cluster_candidates",
        return_value=[{"candidate_id": "cand_002", "decision": "same_cluster", "target_cluster_id": "slot-opening"}],
    ), patch(
        "app.modules.wiki_generation.infrastructure.post_ingest_cluster.judge_cluster_promotions",
        return_value={"cogging-torque": "여러 근거가 쌓임"},
    ) as judge_promotions:
        artifact = build_post_ingest_cluster_artifact(
            completion=MagicMock(),
            normalized=normalized,
            existing_active_markdown=existing,
            user_id="user-1",
            workspace_id="workspace-1",
            concept_update_decisions=[],
            core_relation_decisions=[],
            core_concepts=[{"slug": "back-electromotive-force", "title": "역기전력", "aliases": ["Back EMF"]}],
        )

    # 근거가 덧붙은 클러스터 중 이미 승격 후보인 slot-opening은 다시 묻지 않는다.
    assert [item["cluster_id"] for item in judge_promotions.call_args.kwargs["clusters"]] == ["cogging-torque"]
    clusters = {cluster["id"]: cluster for cluster in artifact["clusters"]}
    assert clusters["back-emf"]["promotion"]["target"] == "back-electromotive-force"
    assert clusters["back-emf"]["promotion"]["source_refs"] == ["doc-a"]
    assert clusters["cogging-torque"]["promotion"]["reason"] == "여러 근거가 쌓임"
    assert clusters["slot-opening"].get("promotion") is None
    assert "target: concept:back-electromotive-force" in artifact["active_markdown"]
