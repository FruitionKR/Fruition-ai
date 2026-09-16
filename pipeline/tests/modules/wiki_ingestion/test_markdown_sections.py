import unittest

from app.modules.wiki_ingestion.infrastructure.markdown_sections import markdown_list_section, markdown_section, markdown_section_lines


class MarkdownSectionsTest(unittest.TestCase):
    def test_extracts_markdown_section_until_next_h2(self) -> None:
        markdown = """# Page

## Definition
First line.

Second line.

## Evidence
- Item
"""

        self.assertEqual(markdown_section(markdown, "Definition"), "First line.\nSecond line.")
        self.assertEqual(markdown_section_lines(markdown, "Definition"), ["First line.", "", "Second line.", ""])

    def test_extracts_list_section_items(self) -> None:
        markdown = """## Evidence
- First
-
- Second
  - Nested
plain item
"""

        self.assertEqual(markdown_list_section(markdown, "Evidence"), ["First", "Second", "Nested", "plain item"])


if __name__ == "__main__":
    unittest.main()
