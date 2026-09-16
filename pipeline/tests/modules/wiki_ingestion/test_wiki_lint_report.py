from app.modules.wiki_ingestion.infrastructure.wiki_lint_report import render_lint_log_markdown


def test_render_lint_log_markdown_uses_given_date_and_change_queues() -> None:
    result = {
        "user_id": "user_1",
        "workspace_id": "workspace_1",
        "active_path": "wiki/user_1/workspace_1/clusters/active.md",
        "orphan_refs": ["doc_1:B0001"],
        "promotion_candidates": ["cluster_1"],
        "needs_review": [],
        "relation_candidates": [],
        "invalid_relations": [],
        "invalid_promotions": [],
        "orphan_link_candidates": [
            {
                "source": "concept:shared",
                "target": "concept:removed",
                "relation": "related_to",
                "reason": "no_active_support",
            }
        ],
        "removed_orphan_links": [
            {
                "source": "concept:shared",
                "target": "concept:removed",
                "relation": "related_to",
                "reason": "no_active_support",
            }
        ],
        "materialized_promotions": [
            {"cluster_id": "cluster_1", "concept_slug": "back-emf"},
        ],
        "merged_promotions": [],
        "materialized_relations": [],
    }

    markdown = render_lint_log_markdown(result, "2026-07-18")

    assert markdown.startswith("## 2026-07-18 lint: workspace_1\n")
    assert "- doc_1:B0001" in markdown
    assert "- cluster:cluster_1" in markdown
    assert (
        "- removed: concept:shared -[related_to]-> concept:removed"
        in markdown
    )
    assert "  reason: no_active_support" in markdown
    assert "- promoted: cluster:cluster_1 -> concept:back-emf" in markdown
