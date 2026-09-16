from app.modules.wiki_ingestion.infrastructure.source_contribution_reconciliation import (
    _reconciliation_candidate,
    _remove_stale_document_concepts,
    apply_structural_reconciliation,
    source_contribution_payload,
)


def test_contribution_payload_keeps_reingest_reconciliation_inputs() -> None:
    payload = source_contribution_payload(
        {
            "concept_pages": [
                {"slug": "kept-concept", "markdown": "# 유지"},
                "runs/concepts/path-concept.md",
            ],
            "links": [
                {
                    "source": "source:doc-1",
                    "target": "concept:kept-concept",
                    "relation": "source_mentions_concept",
                    "label": "저장하지 않는 표시값",
                }
            ],
            "source_block_changes": {
                "invalidated_block_ids": ["B0002"],
            },
            "meaning_clusters": {
                "active_markdown": """# Active Meaning Clusters

## cluster: motor

### Evidence Claims
- claim_1: 최신 주장이다. [doc-1:B0001]

### Core Relation Candidates
- target: concept:target
  relation: supports_or_enables
  evidence: [claim_1]
  reason: 최신 관계
""",
            },
        }
    )

    assert payload == {
        "concept_slugs": ["kept-concept", "path-concept"],
        "links": [
            {
                "source": "source:doc-1",
                "target": "concept:kept-concept",
                "relation": "source_mentions_concept",
            }
        ],
        "source_block_changes": {
            "invalidated_block_ids": ["B0002"],
        },
        "claim_signatures": [
            ["motor", "claim_1", "최신 주장이다. [doc-1:B0001]"],
        ],
        "relation_signatures": [
            [
                "motor",
                "concept:target",
                "supports_or_enables",
                ["claim_1"],
            ]
        ],
    }


def test_contribution_payload_reads_links_json_path(tmp_path) -> None:
    links_path = tmp_path / "links.json"
    links_path.write_text(
        """[
  {
    "source": "source:doc-1",
    "target": "concept:kept-concept",
    "relation": "source_mentions_concept"
  }
]""",
        encoding="utf-8",
    )

    payload = source_contribution_payload({"links": str(links_path)})

    assert payload["links"] == [
        {
            "source": "source:doc-1",
            "target": "concept:kept-concept",
            "relation": "source_mentions_concept",
        }
    ]


def test_reconciliation_candidate_compares_previous_and_current_contribution() -> None:
    candidate = _reconciliation_candidate(
        {
            "pipeline_run_id": "run-2",
            "document_id": "doc-1",
            "user_id": "user-1",
            "workspace_id": "workspace-1",
            "manifest": {
                "source_contribution": {
                    "concept_slugs": ["kept-concept"],
                    "links": [
                        {
                            "source": "source:doc-1",
                            "target": "concept:kept-concept",
                            "relation": "source_mentions_concept",
                        }
                    ],
                    "source_block_changes": {
                        "invalidated_block_ids": ["B0002", "B0003"],
                    },
                    "claim_signatures": [],
                    "relation_signatures": [],
                },
            },
            "previous_manifests": [
                {
                    "concept_pages": [
                        {"slug": "kept-concept"},
                        {"slug": "stale-concept"},
                    ],
                    "links": [
                        {
                            "source": "source:doc-1",
                            "target": "concept:kept-concept",
                            "relation": "source_mentions_concept",
                        },
                        {
                            "source": "source:doc-1",
                            "target": "concept:stale-concept",
                            "relation": "source_mentions_concept",
                        },
                    ],
                }
            ],
            "linked_concept_slugs": ["kept-concept", "stale-concept"],
        }
    )

    assert candidate["invalidated_source_refs"] == [
        "doc-1:B0002",
        "doc-1:B0003",
    ]
    assert candidate["stale_concept_slugs"] == ["stale-concept"]
    assert candidate["stale_relations"] == [
        {
            "source": "source:doc-1",
            "target": "concept:stale-concept",
            "relation": "source_mentions_concept",
        }
    ]
    assert candidate["_current_claim_signatures"] == []
    assert candidate["_current_relation_signatures"] == []
    assert candidate["_cluster_reconciliation_ready"] is True


def test_reconciliation_candidate_reads_legacy_manifest_links() -> None:
    candidate = _reconciliation_candidate(
        {
            "pipeline_run_id": "run-2",
            "document_id": "doc-1",
            "user_id": "user-1",
            "workspace_id": "workspace-1",
            "manifest": {
                "source_contribution": {
                    "concept_slugs": [],
                    "links": [],
                    "source_block_changes": {
                        "invalidated_block_ids": ["B0001"],
                    },
                }
            },
            "previous_manifests": [
                {
                    "concept_pages": [{"slug": "old-concept"}],
                    "links": [
                        {
                            "source": "source:doc-1",
                            "target": "concept:old-concept",
                            "relation": "source_mentions_concept",
                        }
                    ],
                }
            ],
            "linked_concept_slugs": ["old-concept"],
        }
    )

    assert candidate["stale_relations"] == [
        {
            "source": "source:doc-1",
            "target": "concept:old-concept",
            "relation": "source_mentions_concept",
        }
    ]
    assert candidate["_cluster_reconciliation_ready"] is False


def test_structural_reconciliation_keeps_relation_supported_by_another_document() -> None:
    class Result:
        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class FakeConnection:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def execute(self, query: str, _params):
            self.queries.append(" ".join(query.split()))
            return Result()

    relation = {
        "source": "concept:shared-source",
        "target": "concept:shared-target",
        "relation": "supports_or_enables",
    }
    candidate = {
        "pipeline_run_id": "run-2",
        "document_id": "doc-1",
        "user_id": "user-1",
        "workspace_id": "workspace-1",
        "stale_concept_slugs": [],
        "stale_relations": [relation],
        "structural_reconciled": False,
    }
    conn = FakeConnection()

    applied = apply_structural_reconciliation(
        conn,  # type: ignore[arg-type]
        [candidate],
        {("concept:shared-source", "concept:shared-target", "supports_or_enables")},
    )

    assert all("DELETE FROM wiki_page_links" not in query for query in conn.queries)
    assert applied[0]["removed_relations"] == []
    assert any("jsonb_build_object" in query for query in conn.queries)


def test_remove_stale_document_concepts_removes_its_embedding_units() -> None:
    class Result:
        def __init__(self, rows=None, rowcount=0) -> None:
            self._rows = rows or []
            self.rowcount = rowcount

        def fetchall(self):
            return self._rows

    class FakeConnection:
        def __init__(self) -> None:
            self.calls = []

        def execute(self, query: str, params):
            self.calls.append((" ".join(query.split()), params))
            if "DELETE FROM document_wiki_links" in query:
                return Result([{"id": "page-1", "slug": "stale-concept"}])
            return Result(rowcount=3)

    conn = FakeConnection()

    removed_slugs, removed_units = _remove_stale_document_concepts(
        conn,  # type: ignore[arg-type]
        "doc-1",
        ["stale-concept"],
    )

    assert removed_slugs == ["stale-concept"]
    assert removed_units == 3
    assert conn.calls[1][1] == ("doc-1", ["page-1"])
