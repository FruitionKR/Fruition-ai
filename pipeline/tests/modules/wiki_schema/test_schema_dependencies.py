import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.modules.wiki_schema.interfaces.http.dependencies import get_organize_schema_use_case


class WikiSchemaDependenciesTest(unittest.TestCase):
    def setUp(self) -> None:
        get_organize_schema_use_case.cache_clear()

    def tearDown(self) -> None:
        get_organize_schema_use_case.cache_clear()

    def test_returns_503_when_llm_config_is_missing(self) -> None:
        with patch(
            "app.modules.wiki_schema.interfaces.http.dependencies.build_schema_organizer",
            side_effect=RuntimeError("missing key"),
        ):
            with self.assertRaises(HTTPException) as context:
                get_organize_schema_use_case()

        self.assertEqual(context.exception.status_code, 503)
        self.assertEqual(context.exception.detail, "missing key")


if __name__ == "__main__":
    unittest.main()
