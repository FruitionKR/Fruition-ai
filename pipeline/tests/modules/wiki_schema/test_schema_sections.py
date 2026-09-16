import unittest
from dataclasses import fields

from app.modules.wiki_schema.application.schema_sections import SCHEMA_SECTIONS, SCHEMA_SECTION_NAMES
from app.modules.wiki_schema.domain.entities import SchemaFragments


class SchemaSectionsTest(unittest.TestCase):
    def test_section_names_match_schema_fragments_fields(self) -> None:
        fragment_fields = tuple(field.name for field in fields(SchemaFragments))

        self.assertEqual(SCHEMA_SECTION_NAMES, fragment_fields)
        self.assertEqual(tuple(field_name for field_name, _ in SCHEMA_SECTIONS), fragment_fields)

    def test_section_titles_are_present_for_preview_rendering(self) -> None:
        self.assertTrue(all(title for _, title in SCHEMA_SECTIONS))


if __name__ == "__main__":
    unittest.main()
