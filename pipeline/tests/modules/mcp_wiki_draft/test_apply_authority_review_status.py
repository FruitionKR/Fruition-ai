import json
from unittest.mock import MagicMock

import pytest

from app.modules.mcp_wiki_draft.infrastructure import apply_authority


@pytest.mark.parametrize("status", [[], {}, None, 1, True])
def test_malformed_authority_status_is_unknown(monkeypatch, status):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "schema_version": "mcp.wiki-approval-status.v1", "status": status, "reason": None,
    }).encode()
    monkeypatch.setattr(apply_authority, "urlopen", lambda *args, **kwargs: response)
    result = apply_authority.BackendWikiApprovalAuthority("https://document.internal", "token", 1).lookup({})
    assert result == {
        "schema_version": "mcp.wiki-approval-status.v1",
        "status": "UNKNOWN", "reason": "AUTHZ_UNAVAILABLE",
    }
