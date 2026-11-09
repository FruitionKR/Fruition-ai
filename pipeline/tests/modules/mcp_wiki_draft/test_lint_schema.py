from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.modules.mcp_wiki_draft.interfaces.http.schemas import LintOperationRequest


@pytest.mark.parametrize(
    ("kind", "arguments"),
    [
        ("UPSERT_STATEMENT", {"statement_id": None, "text": "Alpha", "evidence": []}),
        ("REMOVE_STATEMENT", {"statement_id": "statement-1", "expected_text_hash": "sha256:" + "0" * 64}),
        ("MERGE_CONCEPTS", {"source_page_ids": ["page-2"], "destination_page_id": "page-1"}),
        ("ADD_LINK", {"source_ref": "concept:alpha", "target_ref": "concept:beta", "relation": "concept_related_to"}),
        ("REMOVE_LINK", {"source_ref": "concept:alpha", "target_ref": "concept:beta", "relation": "concept_related_to"}),
        ("DELETE_ORPHAN_PAGE", {"page_id": "page-1", "expected_content_hash": "sha256:" + "1" * 64}),
    ],
)
def test_closed_lint_operation_shapes(kind, arguments):
    operation = LintOperationRequest.model_validate({
        "operation_id": "operation-1", "kind": kind, "target_page_id": "page-1",
        "arguments": arguments, "depends_on": [],
    })
    assert operation.kind == kind


@pytest.mark.parametrize(
    "value",
    [
        {
            "operation_id": "operation-1", "kind": "FREE_MARKDOWN", "target_page_id": "page-1",
            "arguments": {"markdown": "# arbitrary"}, "depends_on": [],
        },
        {
            "operation_id": "operation-1", "kind": "REMOVE_STATEMENT", "target_page_id": "page-1",
            "arguments": {
                "statement_id": "statement-1", "expected_text_hash": "sha256:" + "0" * 64,
                "evidence": [],
            },
            "depends_on": [],
        },
        {
            "operation_id": "operation-1", "kind": "ADD_LINK", "target_page_id": "page-1",
            "arguments": {
                "source_ref": "concept:alpha", "target_ref": "concept:beta",
                "relation": "concept_related_to", "text": "invented",
            },
            "depends_on": [],
        },
    ],
)
def test_lint_operation_shape_is_exact_and_closed(value):
    with pytest.raises(ValidationError):
        LintOperationRequest.model_validate(value)
