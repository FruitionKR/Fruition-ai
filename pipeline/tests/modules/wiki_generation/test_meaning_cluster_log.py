from app.modules.wiki_generation.infrastructure.meaning_cluster_log import render_meaning_cluster_log


def test_render_meaning_cluster_log_uses_given_date_and_cluster_decisions() -> None:
    clusters = [
        {
            "id": "back-emf",
            "evidence_claims": [
                {
                    "id": "claim_001",
                    "claim": "Back EMF는 전동기 성능 지표다.",
                    "refs": ["doc_a:B0001"],
                    "cluster_decision": "new_cluster",
                    "decision_reason": "새 개념 후보",
                }
            ],
            "core_relation_candidates": [],
            "promotion": None,
        }
    ]

    markdown = render_meaning_cluster_log(
        {"document": {"document_id": "doc_a", "source_path": "motor.md"}},
        clusters,
        "user_1",
        "workspace_1",
        [],
        [],
        "2026-07-18",
    )

    assert markdown.startswith("## 2026-07-18 ingest: doc_a\n")
    assert "claim_001 -> cluster:back-emf" in markdown
    assert "doc_a:B0001" in markdown
