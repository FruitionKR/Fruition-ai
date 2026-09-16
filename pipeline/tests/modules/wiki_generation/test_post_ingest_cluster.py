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
