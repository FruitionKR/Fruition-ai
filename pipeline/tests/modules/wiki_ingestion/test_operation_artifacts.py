import hashlib
import json

from app.modules.wiki_ingestion.infrastructure.operation_artifacts import (
    persist_operation_artifacts,
)


def test_persists_source_snapshot_and_concept_contribution_json() -> None:
    writes: list[tuple[str, str, str]] = []

    artifacts = persist_operation_artifacts(
        operation_id="op-A2",
        workspace_id="ws-1",
        source_page_id="source-page",
        source_markdown="# Source A2\n",
        concept_pages=[
            {
                "page_id": "concept-page",
                "slug": "shared",
                "markdown": "# Shared\n\nA2 근거\n",
            }
        ],
        concept_contributions={
            "shared": {
                "schema_version": 1,
                "operation_id": "op-A2",
                "document_id": "doc-A",
                "concept": {"slug": "shared"},
                "evidence_units": [],
                "source_blocks": [],
                "links": [
                    {
                        "source": "concept:shared",
                        "target": "concept:target",
                        "relation": "related_to",
                    }
                ],
            }
        },
        write_text=lambda key, text, content_type: (
            writes.append((key, text, content_type)) or f"s3://bucket/{key}"
        ),
    )

    assert [item["page_type"] for item in artifacts] == ["source", "concept"]
    assert artifacts[0] == {
        "page_id": "source-page",
        "page_type": "source",
        "markdown_key": "wiki/ws-1/pages/source-page/ops/op-A2.md",
        "content_hash": (
            "sha256:"
            + hashlib.sha256("# Source A2\n".encode("utf-8")).hexdigest()
        ),
    }
    assert artifacts[1]["contribution_key"] == (
        "wiki/ws-1/pages/concept-page/ops/op-A2.json"
    )
    contribution = json.loads(writes[2][1])
    assert contribution["page_id"] == "concept-page"
    assert contribution["concept"]["slug"] == "shared"
    assert contribution["links"] == [
        {
            "source": "concept:shared",
            "target": "concept:target",
            "relation": "related_to",
        }
    ]
    assert writes[2][2] == "application/json; charset=utf-8"


def test_fails_when_changed_concept_has_no_contribution_json() -> None:
    try:
        persist_operation_artifacts(
            operation_id="op-1",
            workspace_id="ws-1",
            source_page_id="source-page",
            source_markdown="# Source\n",
            concept_pages=[
                {
                    "page_id": "concept-page",
                    "slug": "missing",
                    "markdown": "# Missing\n",
                }
            ],
            concept_contributions={},
            write_text=lambda *_args: "s3://bucket/object",
        )
    except ValueError as exc:
        assert "missing concept contribution JSON" in str(exc)
    else:
        raise AssertionError("missing concept contribution must fail")
