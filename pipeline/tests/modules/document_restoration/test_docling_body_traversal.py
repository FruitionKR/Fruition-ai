import unittest
from unittest import mock

from app.modules.document_restoration.infrastructure import (
    build_docling_primary_manifest as module,
)
from app.modules.document_restoration.infrastructure.build_docling_primary_manifest import (
    best_text_for_docling_item,
    ordered_body_refs,
)


class DoclingBodyTraversalTest(unittest.TestCase):
    def test_expands_nested_groups_in_document_order(self) -> None:
        docling = {
            "body": {
                "children": [
                    {"$ref": "#/texts/0"},
                    {"$ref": "#/groups/0"},
                    {"$ref": "#/texts/4"},
                ]
            },
            "groups": [
                {
                    "self_ref": "#/groups/0",
                    "children": [
                        {"$ref": "#/texts/1"},
                        {"$ref": "#/groups/1"},
                        {"$ref": "#/texts/3"},
                    ],
                },
                {
                    "self_ref": "#/groups/1",
                    "children": [{"$ref": "#/texts/2"}],
                },
            ],
        }

        self.assertEqual(
            ordered_body_refs(docling),
            [
                "#/texts/0",
                "#/texts/1",
                "#/texts/2",
                "#/texts/3",
                "#/texts/4",
            ],
        )

    def test_does_not_expand_same_group_twice(self) -> None:
        docling = {
            "body": {
                "children": [
                    {"$ref": "#/groups/0"},
                    {"$ref": "#/groups/0"},
                ]
            },
            "groups": [
                {
                    "self_ref": "#/groups/0",
                    "children": [{"$ref": "#/texts/0"}],
                }
            ],
        }

        self.assertEqual(ordered_body_refs(docling), ["#/texts/0"])

    def test_keeps_docling_text_for_list_item(self) -> None:
        item = {
            "label": "list_item",
            "text": "[1] Correct reference text.",
            "prov": [
                {
                    "page_no": 1,
                    "bbox": {"l": 10, "t": 20, "r": 100, "b": 30},
                }
            ],
        }

        with mock.patch.object(module, "auxiliary_text_for_block") as auxiliary_text:
            text, candidates, needs_adjudication = best_text_for_docling_item(
                item,
                {"1": {"size": {"height": 200}}},
                [{"type": "paragraph"}],
            )

        auxiliary_text.assert_not_called()
        self.assertEqual(text, "[1] Correct reference text.")
        self.assertEqual(candidates[0]["source"], "docling")
        self.assertFalse(needs_adjudication)
