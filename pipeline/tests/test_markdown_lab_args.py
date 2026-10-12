import sys
from unittest.mock import patch

import markdown_edit_gfm_lab


def test_markdown_labs_default_to_openai_contract(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    for module in (markdown_edit_gfm_lab,):
        with patch.object(sys, "argv", [module.__name__]):
            args = module.parse_args()

        assert not hasattr(args, "endpoint")
        assert args.api_key == "test-openai-key"
        assert args.model == "gpt-6-luna"


def test_markdown_labs_build_clients_without_legacy_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    with (
        patch.object(sys, "argv", [markdown_edit_gfm_lab.__name__]),
        patch.object(markdown_edit_gfm_lab, "CASES", ()),
    ):
        markdown_edit_gfm_lab.main()
