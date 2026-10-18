import unittest

from app.modules.skill.domain.entities import SkillCapability
from app.modules.skill.domain.policy import (
    canonical_allowed_tools,
    validate_allowed_tools,
    with_required_planning_reads,
)


class SkillPolicyTest(unittest.TestCase):
    def test_accepts_tools_allowed_by_capability(self) -> None:
        validate_allowed_tools(
            capabilities=("folder-organize",),
            allowed_tools=("list_root_items", "list_folder_children", "move_document"),
        )

    def test_rejects_tool_outside_capability(self) -> None:
        with self.assertRaisesRegex(ValueError, "allowed_tools"):
            validate_allowed_tools(
                capabilities=("folder-organize",),
                allowed_tools=("delete_folder",),
            )

    def test_document_capabilities_allow_only_their_document_tools(self) -> None:
        validate_allowed_tools(
            capabilities=("document-create",),
            allowed_tools=(
                "list_root_items",
                "list_folder_children",
                "get_document_content",
                "create_document",
            ),
        )
        validate_allowed_tools(
            capabilities=("document-edit",),
            allowed_tools=(
                "list_root_items",
                "list_folder_children",
                "get_document_content",
                "apply_document_edit",
            ),
        )

        with self.assertRaisesRegex(ValueError, "allowed_tools"):
            validate_allowed_tools(
                capabilities=("document-create",),
                allowed_tools=("apply_document_edit",),
            )

    def test_mutation_tools_require_hierarchy_reads(self) -> None:
        with self.assertRaisesRegex(ValueError, "planning read"):
            validate_allowed_tools(
                capabilities=("folder-organize",),
                allowed_tools=("move_document",),
            )

    def test_read_only_tools_are_preserved_without_planning_reads(self) -> None:
        self.assertEqual(
            with_required_planning_reads(("get_document_content",)),
            ("get_document_content",),
        )

    def test_canonical_allowed_tools_is_deterministic_and_valid(self) -> None:
        for capabilities in (
            ("document-create",),
            ("document-edit",),
            ("folder-organize",),
            ("template",),
            ("document-create", "document-edit"),
            ("document-edit", "folder-organize"),
        ):
            tools = canonical_allowed_tools(capabilities)
            self.assertEqual(tools, canonical_allowed_tools(tuple(reversed(capabilities))))
            self.assertEqual(len(tools), len(set(tools)))
            self.assertEqual(tools[:2], ("list_root_items", "list_folder_children"))
            validate_allowed_tools(capabilities, tools)

    def test_canonical_allowed_tools_keeps_capability_boundaries(self) -> None:
        self.assertNotIn("apply_document_edit", canonical_allowed_tools(("document-create",)))
        self.assertNotIn("create_document", canonical_allowed_tools(("document-edit",)))

    def test_defines_only_mvp_capabilities(self) -> None:
        capabilities: tuple[SkillCapability, ...] = (
            "document-create",
            "document-edit",
            "folder-organize",
            "template",
        )

        self.assertEqual(len(capabilities), 4)


if __name__ == "__main__":
    unittest.main()
